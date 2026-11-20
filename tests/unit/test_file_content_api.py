"""Credential-free byte transfers retain upload ownership and publication guards."""

from __future__ import annotations

import hashlib
import io
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import BinaryIO

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from heliopuddle.server.api import create_api
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import FileStorageLocation
from heliopuddle.storage.location import StorageLocation
from tests.lifecycle import patch_upload, upload_attr
from tests.local_auth import add_user, bearer, issue_token, local_context

pytestmark = pytest.mark.req("AUTH-07", "AUTH-08", "AUTH-15", "STO-09", "STO-10")


@pytest.fixture
def api(
    tmp_path: Path, config_dir: Path, migrated_db: Path, inline_prefect: None
) -> Iterator[TestClient]:
    ctx = local_context(tmp_path, config_dir, migrated_db)
    for name, roles in (
        ("writer", ["hp-writer:demo"]),
        ("reader", ["hp-reader:demo"]),
        ("outsider", ["hp-writer:other"]),
    ):
        add_user(ctx, name, roles)
    with TestClient(create_api(ctx)) as client:
        client.headers.update(bearer(issue_token(ctx, "writer")))
        yield client
    ctx.dispose()


def reserve(api: TestClient, data: bytes = b"content", **extra: object) -> dict:
    response = api.post(
        "/projects/demo/uploads",
        json={
            "path": "content.dat",
            "hash": hashlib.md5(data, usedforsecurity=False).hexdigest(),
            "size": len(data),
            **extra,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def token_for(api: TestClient, username: str) -> dict[str, str]:
    ctx: AppContext = api.app.state.ctx  # type: ignore[union-attr]
    return bearer(issue_token(ctx, username))


def test_writer_uploads_reader_downloads_without_credentials(api: TestClient, store: Path) -> None:
    reservation = reserve(api)
    token = reservation["token"]
    response = api.put(f"/uploads/{token}/content", content=iter([b"con", b"tent"]))
    assert response.status_code == 204, response.text
    assert not (store / "content.dat").exists()
    assert list(store.glob("*.hp-tmp"))
    done = api.post(f"/uploads/{token}/complete")
    assert done.status_code == 200, done.text
    assert done.json()["state"] == "available"
    assert (store / "content.dat").read_bytes() == b"content"
    assert not list(store.glob("*.hp-tmp"))
    api.headers.update(token_for(api, "reader"))
    response = api.get(f"/files/{done.json()['id']}/content")
    assert response.status_code == 200
    assert response.content == b"content"
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["cache-control"] == "no-store"
    assert api.get("/projects/demo/storage-locations/primary").status_code == 403
    assert api.put(f"/uploads/{token}/content", content=b"content").status_code in (403, 404)


@pytest.mark.parametrize(
    ("data", "status"), [(b"too much content", 413), (b"short", 422), (b"invalid", 422)]
)
def test_bad_body_never_changes_original(
    api: TestClient, store: Path, data: bytes, status: int
) -> None:
    reservation = reserve(api)
    token = reservation["token"]
    response = api.put(f"/uploads/{token}/content", content=data)
    assert response.status_code == status, response.text
    assert not list(store.rglob("*.hp-tmp"))
    assert not (store / "content.dat").exists()
    assert api.post(f"/uploads/{token}/renew").status_code == 200
    assert api.delete(f"/uploads/{token}").status_code == 204


def test_overwrite_keeps_original_until_complete(api: TestClient, store: Path) -> None:
    token = reserve(api)["token"]
    assert api.put(f"/uploads/{token}/content", content=b"content").status_code == 204
    record = api.post(f"/uploads/{token}/complete").json()
    token = reserve(api, b"replacement", overwrite=True)["token"]
    assert api.put(f"/uploads/{token}/content", content=b"replacement").status_code == 204
    assert api.get(f"/files/{record['id']}/content").content == b"content"
    assert (store / "content.dat").read_bytes() == b"content"
    assert api.post(f"/uploads/{token}/complete").json()["state"] == "available"
    assert api.get(f"/files/{record['id']}/content").content == b"replacement"
    assert api.put(f"/uploads/{token}/content", content=b"replacement").status_code in (404, 409)


def test_content_routes_hide_other_projects_and_reject_missing_auth(api: TestClient) -> None:
    token = reserve(api)["token"]
    assert api.put(f"/uploads/{token}/content", content=b"content").status_code == 204
    record = api.post(f"/uploads/{token}/complete").json()
    another = reserve(api, path="another.dat")["token"]
    api.headers.update(token_for(api, "outsider"))
    assert api.get(f"/files/{record['id']}/content").status_code == 404
    assert api.put(f"/uploads/{another}/content", content=b"content").status_code == 404
    api.headers.pop("authorization")
    assert api.get(f"/files/{record['id']}/content").status_code == 401
    assert api.put(f"/uploads/{another}/content", content=b"content").status_code == 401


def test_read_missing_or_unpublished_content_is_404(api: TestClient, store: Path) -> None:
    reservation = reserve(api)
    file_id = reservation["file"]["id"]
    assert api.get(f"/files/{file_id}/content").status_code == 404
    token = reservation["token"]
    assert api.put(f"/uploads/{token}/content", content=b"content").status_code == 204
    assert api.post(f"/uploads/{token}/complete").status_code == 200
    (store / "content.dat").unlink()
    assert api.get(f"/files/{file_id}/content").status_code == 404


def test_backend_error_is_sanitized_and_reservation_can_retry(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = reserve(api)["token"]
    original = StorageLocation.write

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("private-backend-credential")

    monkeypatch.setattr(StorageLocation, "write", fail)
    failed = api.put(f"/uploads/{token}/content", content=b"content")
    assert failed.status_code == 503
    assert "private-backend-credential" not in failed.text
    monkeypatch.setattr(StorageLocation, "write", original)
    assert api.put(f"/uploads/{token}/content", content=b"content").status_code == 204
    assert api.post(f"/uploads/{token}/complete").json()["state"] == "available"


@pytest.mark.parametrize("data", [b"", b"x" * (8 * 1024 * 1024 + 19)])
def test_empty_and_multichunk_content(api: TestClient, data: bytes) -> None:
    token = reserve(api, data)["token"]
    assert api.put(f"/uploads/{token}/content", content=data).status_code == 204
    done = api.post(f"/uploads/{token}/complete")
    assert done.status_code == 200, done.text
    assert api.get(f"/files/{done.json()['id']}/content").content == data


def test_write_claim_excludes_concurrent_operations(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = reserve(api)["token"]
    write = StorageLocation.write

    def checked_write(
        store: StorageLocation,
        path: str,
        source: BinaryIO,
        *,
        check: Callable[[], None] | None = None,
    ) -> None:
        for method, suffix in (("POST", "/renew"), ("POST", "/complete"), ("DELETE", "")):
            response = api.request(method, f"/uploads/{token}{suffix}")
            assert response.status_code in (404, 409), response.text
        write(store, path, source, check=check)

    monkeypatch.setattr(StorageLocation, "write", checked_write)
    assert api.put(f"/uploads/{token}/content", content=b"content").status_code == 204
    assert api.post(f"/uploads/{token}/complete").json()["state"] == "available"


def test_stale_write_cannot_reclaim_or_commit(
    api: TestClient, store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reservation = reserve(api)
    token = reservation["token"]
    ctx: AppContext = api.app.state.ctx  # type: ignore[union-attr]
    original = upload_attr("store_for")

    def take_over(context: AppContext, project: str, storage_location: str) -> StorageLocation:
        with ctx.write_session() as session:
            copy = session.scalars(
                select(FileStorageLocation).where(
                    FileStorageLocation.file_id == reservation["file"]["id"]
                )
            ).one()
            copy.op_token = "replacement-owner"
        return original(context, project, storage_location)

    patch_upload(monkeypatch, "store_for", take_over)
    result = api.put(f"/uploads/{token}/content", content=b"content")
    assert result.status_code == 409, result.text
    assert not list(store.rglob("*.hp-tmp"))
    assert not (store / "content.dat").exists()
    with ctx.sessionmaker() as session:
        copy = session.scalars(
            select(FileStorageLocation).where(
                FileStorageLocation.file_id == reservation["file"]["id"]
            )
        ).one()
        assert copy.op_token == "replacement-owner"


@pytest.mark.parametrize("fail_after_first_chunk", [False, True])
def test_read_errors_close_stream_and_do_not_disclose_backend_secrets(
    api: TestClient, monkeypatch: pytest.MonkeyPatch, fail_after_first_chunk: bool
) -> None:
    token = reserve(api)["token"]
    assert api.put(f"/uploads/{token}/content", content=b"content").status_code == 204
    record = api.post(f"/uploads/{token}/complete").json()

    class BrokenStream(io.BytesIO):
        reads = 0

        def read(self, size: int = -1) -> bytes:
            self.reads += 1
            if fail_after_first_chunk and self.reads == 1:
                return b"content"
            raise OSError("private-backend-credential")

    stream = BrokenStream()
    monkeypatch.setattr(StorageLocation, "open_read", lambda *args: stream)
    if fail_after_first_chunk:
        with pytest.raises(OSError, match=r"^backing storage read failed$"):
            api.get(f"/files/{record['id']}/content")
    else:
        response = api.get(f"/files/{record['id']}/content")
        assert response.status_code == 503
        assert "private-backend-credential" not in response.text
    assert stream.closed

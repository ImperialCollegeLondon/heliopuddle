"""``GET /files/{id}/preview`` (API-18): auth, copy choice, failures, cache and the size setting."""

from __future__ import annotations

import io
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import fsspec
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from heliopuddle.server.api import create_api
from heliopuddle.server.context import AppContext
from heliopuddle.server.schemas import FilePreview, FileState
from heliopuddle.server.services import preview as preview_service
from heliopuddle.settings import Settings
from heliopuddle.storage.location import StorageLocation
from tests.golden_files import add_file
from tests.local_auth import add_user, bearer, issue_token, local_context
from tests.unit.test_preview_cdf import write_sample

pytestmark = pytest.mark.req("API-18")

CSV = b"time,value\n1,2.5\n2,3.5\n"


@pytest.fixture(autouse=True)
def _empty_cache() -> Iterator[None]:
    preview_service.cache.clear()
    yield
    preview_service.cache.clear()


@pytest.fixture
def ctx(tmp_path: Path, config_dir: Path, migrated_db: Path) -> Iterator[AppContext]:
    context = local_context(tmp_path, config_dir, migrated_db)
    for name, roles in (("reader", ["hp-reader:demo"]), ("outsider", ["hp-writer:other"])):
        add_user(context, name, roles)
    yield context
    context.dispose()


@pytest.fixture
def api(ctx: AppContext) -> Iterator[TestClient]:
    with TestClient(create_api(ctx)) as client:
        client.headers.update(bearer(issue_token(ctx, "reader")))
        yield client


def put(
    ctx: AppContext,
    store: Path,
    path: str,
    data: bytes,
    copies: dict[str, FileState] | None = None,
    **kwargs: Any,
) -> int:
    target = store / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    with ctx.write_session() as session:
        return add_file(
            session,
            "demo",
            path,
            copies=copies or {"primary": FileState.AVAILABLE},
            size=len(data),
            **kwargs,
        )


def test_reader_gets_a_preview(api: TestClient, ctx: AppContext, store: Path) -> None:
    file_id = put(ctx, store, "d/t.csv", CSV)
    response = api.get(f"/files/{file_id}/preview")
    assert response.status_code == 200, response.text
    p = FilePreview.model_validate(response.json())
    assert (p.file_id, p.kind, p.format, p.storage_location) == (file_id, "table", "csv", "primary")
    assert p.tables[0].rows == [["1", "2.5"], ["2", "3.5"]]
    assert p.max_rows == 100


def test_preview_auth_matches_other_file_routes(
    api: TestClient, ctx: AppContext, store: Path
) -> None:
    file_id = put(ctx, store, "t.csv", CSV)
    api.headers.update(bearer(issue_token(ctx, "outsider")))
    assert api.get(f"/files/{file_id}/preview").status_code == 404
    api.headers.pop("authorization")
    assert api.get(f"/files/{file_id}/preview").status_code == 401


def test_deleted_unknown_or_unreadable_files_are_404(
    api: TestClient, ctx: AppContext, store: Path
) -> None:
    deleted = put(ctx, store, "deleted.csv", CSV, deleted=True, latest=False)
    missing = put(ctx, store, "missing.csv", CSV, copies={"primary": FileState.MISSING})
    pending = put(ctx, store, "pending.csv", CSV, copies={"primary": FileState.PENDING})
    gone = put(ctx, store, "gone.csv", CSV)
    (store / "gone.csv").unlink()
    for file_id in (deleted, missing, pending, gone, 999_999):
        response = api.get(f"/files/{file_id}/preview")
        assert response.status_code == 404, (file_id, response.text)


def test_overwriting_copy_is_read_when_none_is_available(
    api: TestClient, ctx: AppContext, store: Path
) -> None:
    file_id = put(ctx, store, "o.txt", b"original\n", copies={"primary": FileState.OVERWRITING})
    p = api.get(f"/files/{file_id}/preview").json()
    assert (p["kind"], p["text"], p["storage_location"]) == ("text", "original\n", "primary")


def test_unparseable_content_is_200_with_kind_error(
    api: TestClient, ctx: AppContext, store: Path
) -> None:
    file_id = put(ctx, store, "bad.json", b"{oops")
    response = api.get(f"/files/{file_id}/preview")
    assert response.status_code == 200
    assert response.json()["kind"] == "error"
    assert str(store) not in response.text


def test_image_and_unknown_formats(api: TestClient, ctx: AppContext, store: Path) -> None:
    image = put(ctx, store, "plot.PNG", b"\x89PNG")
    other = put(ctx, store, "blob.bin", b"\x00")
    assert api.get(f"/files/{image}/preview").json()["media_type"] == "image/png"
    assert api.get(f"/files/{other}/preview").json()["kind"] == "none"


@pytest.mark.parametrize("method", ["open_read", "info"])
def test_storage_failure_is_503_without_details(
    api: TestClient, ctx: AppContext, store: Path, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    file_id = put(ctx, store, "t.csv", CSV)

    def fail(*args: object) -> Any:
        raise OSError("private-backend-credential")

    monkeypatch.setattr(StorageLocation, method, fail)
    response = api.get(f"/files/{file_id}/preview")
    assert response.status_code == 503
    assert "private-backend-credential" not in response.text


def test_read_failure_mid_stream_is_503(
    api: TestClient, ctx: AppContext, store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_id = put(ctx, store, "t.txt", b"text\n")

    class Broken(io.RawIOBase):
        def readable(self) -> bool:
            return True

        def readinto(self, buffer: Any) -> int:
            raise TimeoutError("private-backend-credential")

    monkeypatch.setattr(StorageLocation, "open_read", lambda *args: Broken())
    response = api.get(f"/files/{file_id}/preview")
    assert response.status_code == 503
    assert "private-backend-credential" not in response.text


def test_previews_are_cached_until_the_copy_changes(
    api: TestClient, ctx: AppContext, store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_id = put(ctx, store, "c.txt", b"one\n")
    opened: list[str] = []
    original = StorageLocation.open_read

    def counting(self: StorageLocation, path: str) -> Any:
        opened.append(path)
        return original(self, path)

    monkeypatch.setattr(StorageLocation, "open_read", counting)
    assert api.get(f"/files/{file_id}/preview").json()["text"] == "one\n"
    assert api.get(f"/files/{file_id}/preview").json()["text"] == "one\n"
    assert len(opened) == 1

    target = store / "c.txt"
    target.write_bytes(b"three\n")  # new size
    assert api.get(f"/files/{file_id}/preview").json()["text"] == "three\n"
    assert len(opened) == 2

    stat = target.stat()
    target.write_bytes(b"seven\n")  # same size, new modification time
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    assert api.get(f"/files/{file_id}/preview").json()["text"] == "seven\n"
    assert len(opened) == 3


def test_cache_keeps_the_most_recent_previews() -> None:
    cache = preview_service._Cache(2)
    previews = [FilePreview(file_id=i, kind="none") for i in range(3)]
    cache.put("a", previews[0])
    cache.put("b", previews[1])
    assert cache.get("a") is previews[0]  # "a" is now the most recent
    cache.put("c", previews[2])
    assert cache.get("b") is None
    assert cache.get("a") is previews[0]
    assert cache.get("c") is previews[2]


# -- a storage location without local paths ---------------------------------------

MEMORY_YAML = """\
project: demo
storage_locations:
  - name: primary
    url: memory://{root}
"""


@pytest.fixture
def memory_root() -> Iterator[str]:
    root = f"hp-preview-{uuid.uuid4().hex}"
    yield root
    fs = fsspec.filesystem("memory")
    if fs.exists(f"/{root}"):
        fs.rm(f"/{root}", recursive=True)


@pytest.mark.parametrize(("max_bytes", "kind"), [(100, "too_large"), (10_000_000, "table")])
def test_remote_cdf_is_copied_only_within_the_preview_limit(
    tmp_path: Path, migrated_db: Path, memory_root: str, max_bytes: int, kind: str
) -> None:
    config = tmp_path / "memory-config"
    config.mkdir()
    (config / "demo.yaml").write_text(MEMORY_YAML.format(root=memory_root))
    ctx = local_context(tmp_path, config, migrated_db, preview_max_bytes=max_bytes)
    add_user(ctx, "reader", ["hp-reader:demo"])
    sample = tmp_path / "s.cdf"
    write_sample(sample)
    data = sample.read_bytes()
    fsspec.filesystem("memory").pipe_file(f"/{memory_root}/x/s.cdf", data)
    with ctx.write_session() as session:
        file_id = add_file(
            session, "demo", "x/s.cdf", copies={"primary": FileState.AVAILABLE}, size=len(data)
        )
    try:
        with TestClient(create_api(ctx)) as client:
            client.headers.update(bearer(issue_token(ctx, "reader")))
            response = client.get(f"/files/{file_id}/preview")
    finally:
        ctx.dispose()
    assert response.status_code == 200, response.text
    assert response.json()["kind"] == kind


# -- HELIOPUDDLE_PREVIEW_MAX_BYTES -----------------------------------------------


def test_preview_max_bytes_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings().preview_max_bytes == 1_000_000_000
    monkeypatch.setenv("HELIOPUDDLE_PREVIEW_MAX_BYTES", "2MiB")
    assert Settings().preview_max_bytes == 2 * 1024 * 1024
    monkeypatch.setenv("HELIOPUDDLE_PREVIEW_MAX_BYTES", "500MB")
    assert Settings().preview_max_bytes == 500_000_000
    for bad in ("0", "lots", "-1"):
        monkeypatch.setenv("HELIOPUDDLE_PREVIEW_MAX_BYTES", bad)
        with pytest.raises(ValidationError):
            Settings()

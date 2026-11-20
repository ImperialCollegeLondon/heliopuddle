"""Accept an unknown copy as it is now (API-11 core, D4) through the task and the API."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from heliopuddle.server import models
from heliopuddle.server.api import create_api
from heliopuddle.server.context import AppContext
from heliopuddle.server.models import accept as accept_db
from heliopuddle.server.models import index as index_db
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services.accept import (
    AmbiguousCopyError,
    NotUnknownError,
    accept_copy,
)
from heliopuddle.storage import StorageLocation

pytestmark = pytest.mark.usefixtures("inline_prefect")


@pytest.fixture
def client(ctx: AppContext) -> Iterator[TestClient]:
    with TestClient(create_api(ctx), headers={"X-Forwarded-User": "olivia"}) as c:
        yield c


@pytest.fixture
def store2(tmp_path: Path) -> Path:
    return tmp_path / "store2"


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _write(root: Path, path: str, data: bytes) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def _unknown(ctx: AppContext, root: Path, location: str, path: str, data: bytes) -> int:
    """A file whose copy in ``location`` is unknown (registered hash differs from the store)."""
    _write(root, path, data)
    with ctx.write_session() as session:
        result = models.register_file(
            session,
            project="demo",
            storage_location=location,
            path=path,
            hash=_md5(b"registered"),
            size=10,
        )
        models.set_copy_state(session, result.copy_id, FileState.UNKNOWN)
    return result.file_id


def _available(ctx: AppContext, root: Path, path: str, data: bytes) -> int:
    _write(root, path, data)
    with ctx.write_session() as session:
        result = models.register_file(
            session,
            project="demo",
            storage_location="primary",
            path=path,
            hash=_md5(data),
            size=len(data),
        )
        models.set_copy_verified(
            session, result.copy_id, size=len(data), mtime=None, hash=_md5(data)
        )
    return result.file_id


@pytest.mark.req("API-11")
@pytest.mark.req("API-12")
def test_accept_makes_unknown_copy_available_and_latest(
    ctx: AppContext, client: TestClient, store: Path
) -> None:
    v1 = _available(ctx, store, "a/x_v1.dat", b"one")
    v2 = _unknown(ctx, store, "primary", "a/x_v2.dat", b"two as found")
    assert client.get(f"/files/{v2}").json()["is_latest"] is False
    assert client.get(f"/files/{v1}").json()["is_latest"] is True

    r = client.post(f"/files/{v2}/accept", json={})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["hash"] == _md5(b"two as found")
    assert body["size"] == len(b"two as found")
    assert body["updated_by"] == "olivia"
    assert body["is_latest"] is True
    (copy,) = body["storage_locations"]
    assert copy["state"] == "available"
    assert copy["hash"] == _md5(b"two as found")
    assert copy["mtime"] is not None and copy["last_seen_at"] is not None
    assert client.get(f"/files/{v1}").json()["is_latest"] is False


@pytest.mark.req("API-11")
def test_accept_named_storage_location_marks_other_copies_unknown(
    ctx: AppContext, client: TestClient, store: Path, store2: Path
) -> None:
    file_id = _available(ctx, store, "b/y.dat", b"old!")
    _write(store2, "b/y.dat", b"new content")
    with ctx.write_session() as session:
        result = models.register_file(
            session,
            project="demo",
            storage_location="secondary",
            path="b/y.dat",
            hash=_md5(b"old!"),
            size=4,
        )
        models.set_copy_state(session, result.copy_id, FileState.UNKNOWN)

    r = client.post(f"/files/{file_id}/accept", json={"storage_location": "secondary"})

    assert r.status_code == 200, r.text
    states = {c["storage_location"]: c["state"] for c in r.json()["storage_locations"]}
    assert states == {"primary": "unknown", "secondary": "available"}
    assert r.json()["is_latest"] is True


@pytest.mark.req("API-11")
def test_accept_same_content_keeps_other_copies(
    ctx: AppContext, client: TestClient, store: Path, store2: Path
) -> None:
    file_id = _available(ctx, store, "s/y.dat", b"same")
    _write(store2, "s/y.dat", b"same")
    with ctx.write_session() as session:
        result = models.register_file(
            session,
            project="demo",
            storage_location="secondary",
            path="s/y.dat",
            hash=_md5(b"same"),
            size=4,
        )
        models.set_copy_state(session, result.copy_id, FileState.UNKNOWN)
    r = client.post(f"/files/{file_id}/accept", json={"storage_location": "secondary"})
    states = {c["storage_location"]: c["state"] for c in r.json()["storage_locations"]}
    assert states == {"primary": "available", "secondary": "available"}


@pytest.mark.req("API-11")
def test_accept_errors(ctx: AppContext, client: TestClient, store: Path, store2: Path) -> None:
    ok = _available(ctx, store, "e/ok.dat", b"fine")
    url = f"/files/{ok}/accept"
    assert client.post(url).status_code == 422  # no unknown copy
    assert client.post(url, json={"storage_location": "primary"}).status_code == 409
    assert client.post(url, json={"storage_location": "secondary"}).status_code == 409
    assert client.post(url, json={"storage_location": "nope"}).status_code == 404
    assert client.post("/files/9999/accept").status_code == 404
    assert client.post(url, json={"bogus": 1}).status_code == 422

    two = _unknown(ctx, store, "primary", "e/two.dat", b"x")
    _unknown(ctx, store2, "secondary", "e/two.dat", b"x")
    r = client.post(f"/files/{two}/accept")
    assert r.status_code == 422
    assert "2 unknown copies" in r.json()["detail"]

    gone = _unknown(ctx, store, "primary", "e/gone.dat", b"x")
    (store / "e/gone.dat").unlink()
    assert client.post(f"/files/{gone}/accept").status_code == 409
    assert client.get(f"/files/{gone}").json()["storage_locations"][0]["state"] == "unknown"

    assert client.delete(f"/files/{ok}").status_code == 200
    assert client.post(url).status_code == 404


@pytest.mark.req("API-11")
def test_accept_copy_task_errors(ctx: AppContext, store: Path) -> None:
    file_id = _unknown(ctx, store, "primary", "t/a.dat", b"a")
    with pytest.raises(models.NotFoundError):
        accept_copy.fn(4242)
    with pytest.raises(AmbiguousCopyError):
        accept_copy.fn(_available(ctx, store, "t/b.dat", b"b"))
    with pytest.raises(NotUnknownError):
        accept_copy.fn(file_id, "secondary")

    # Not in the loaded config: LookupError from the task, 409 through the API.
    del ctx.projects["demo"].storage_locations[0]
    with pytest.raises(LookupError, match="not in config"):
        accept_copy.fn(file_id)
    with TestClient(create_api(ctx)) as c:
        assert c.post(f"/files/{file_id}/accept").status_code == 409


@pytest.mark.req("API-11")
def test_accept_rechecks_state_in_the_write_transaction(
    ctx: AppContext, store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_id = _unknown(ctx, store, "primary", "r/a.dat", b"a")
    with ctx.sessionmaker() as session:
        target = index_db.get_accept_target(session, file_id)
    real_md5 = StorageLocation.md5

    def meanwhile(self: StorageLocation, path: str) -> str:
        with ctx.write_session() as session:
            models.set_copy_state(session, target.copy_id, FileState.AVAILABLE)
        return real_md5(self, path)

    monkeypatch.setattr(StorageLocation, "md5", meanwhile)
    with pytest.raises(NotUnknownError):
        accept_copy.fn(file_id)
    monkeypatch.undo()

    # A file deleted after the copy was chosen: 404, not 409.
    with ctx.write_session() as session:
        models.set_copy_state(session, target.copy_id, FileState.UNKNOWN)
    with ctx.sessionmaker() as session:
        chosen = accept_db.choose(session, file_id)
    with ctx.write_session() as session:
        _, copies = models.begin_delete(session, file_id)
    with ctx.write_session() as session:
        models.finish_delete(session, file_id, copies)
    with ctx.write_session() as session, pytest.raises(models.NotFoundError):
        accept_db.accept_copy_record(session, chosen, size=1, mtime=None, hash="h", user=None)


@pytest.mark.req("SVC-03")
def test_forged_native_md5_is_never_trusted_by_verify_or_accept(
    ctx: AppContext, store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backend-reported md5 is only an indexing shortcut: verify and accept read the content."""
    from heliopuddle.server.services.verify import verify_file
    from heliopuddle.storage import backends

    forged = _md5(b"registered")
    monkeypatch.setattr(backends, "native_md5", lambda info: forged)
    file_id = _unknown(ctx, store, "primary", "forged/x.dat", b"actual content")
    with ctx.sessionmaker() as session:
        file = models.get_file(session, file_id)
        assert file is not None
        copy_id = file.copies[0].id
    assert StorageLocation(f"file://{store}").native_md5("forged/x.dat") == forged

    assert verify_file.fn(copy_id) == FileState.UNKNOWN  # the forged md5 matches; content does not

    accept_copy.fn(file_id, "primary")
    with ctx.sessionmaker() as session:
        file = models.get_file(session, file_id)
        assert file is not None
        assert file.hash == _md5(b"actual content")  # read, not the forged md5
        assert [c.hash for c in file.copies] == [_md5(b"actual content")]

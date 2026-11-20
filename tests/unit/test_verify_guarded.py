"""Guarded verify and accept: stale results are dropped (SVC-11, SVC-16).

The competing operation is injected while the object is being read, then the
store and the DB are checked together.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from heliopuddle.server import models
from heliopuddle.server.api import create_api
from heliopuddle.server.api.files import register as register_api
from heliopuddle.server.context import AppContext
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services import accept as accept_mod
from heliopuddle.server.services import verify as verify_mod
from heliopuddle.server.services.accept import NotUnknownError, accept_copy
from heliopuddle.server.services.delete import delete_file
from heliopuddle.server.services.verify import verify_file
from heliopuddle.storage import StorageLocation
from tests.lifecycle import assert_consistent, copies, md5

pytestmark = pytest.mark.usefixtures("inline_prefect")


@pytest.fixture
def client(ctx: AppContext) -> Iterator[TestClient]:
    with TestClient(create_api(ctx)) as c:
        yield c


def _upload(client: TestClient, store: Path, path: str, data: bytes, **extra: Any) -> dict:
    res = client.post(
        "/projects/demo/uploads",
        json={"path": path, "hash": md5(data), "size": len(data)} | extra,
    ).json()
    (store / res["write_path"]).parent.mkdir(parents=True, exist_ok=True)
    (store / res["write_path"]).write_bytes(data)
    r = client.post(f"/uploads/{res['token']}/complete")
    assert r.status_code == 200, r.text
    return r.json()


def _while_observing(monkeypatch: pytest.MonkeyPatch, module: Any, hook: Any) -> None:
    """Run ``hook()`` once when ``module.observe`` is first called, before it reads."""
    real = module.observe
    state = {"done": False}

    def wrapped(store: StorageLocation, path: str) -> Any:
        if not state["done"]:
            state["done"] = True
            hook()
        return real(store, path)

    monkeypatch.setattr(module, "observe", wrapped)


@pytest.mark.req("SVC-11")
def test_verify_racing_an_overwrite_is_dropped(
    ctx: AppContext, client: TestClient, store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _upload(client, store, "v.dat", b"old")
    copy_id = copies(ctx)[0].copy_id
    _while_observing(
        monkeypatch,
        verify_mod,
        lambda: _upload(client, store, "v.dat", b"brand new", overwrite=True),
    )
    # The verify started from the old hash; the store now has the new content.
    assert verify_file.fn(copy_id) == FileState.AVAILABLE
    (row,) = copies(ctx)
    assert (row.state, row.file_hash, row.copy_hash) == (
        FileState.AVAILABLE,
        md5(b"brand new"),
        md5(b"brand new"),
    )
    assert client.get(f"/files/{rec['id']}").json()["hash"] == md5(b"brand new")
    assert_consistent(ctx)


@pytest.mark.req("SVC-11")
def test_verify_racing_a_delete_is_dropped(
    ctx: AppContext, client: TestClient, store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _upload(client, store, "d.dat", b"doomed")
    copy_id = copies(ctx)[0].copy_id
    _while_observing(monkeypatch, verify_mod, lambda: delete_file.fn(rec["id"]))
    assert verify_file.fn(copy_id) == FileState.DELETED
    assert copies(ctx)[0].state == FileState.DELETED
    assert client.get(f"/files/{rec['id']}").json()["deleted_at"] is not None


@pytest.mark.req("SVC-11")
def test_verify_skips_an_owned_copy_and_a_stale_generation(
    ctx: AppContext, client: TestClient, store: Path
) -> None:
    _upload(client, store, "o.dat", b"old")
    (row,) = copies(ctx)
    res = client.post(
        "/projects/demo/uploads",
        json={"path": "o.dat", "hash": md5(b"new"), "size": 3, "overwrite": True},
    ).json()
    assert verify_file.fn(row.copy_id) == FileState.OVERWRITING
    client.delete(f"/uploads/{res['token']}")
    with ctx.sessionmaker() as session:
        generation = models.get_copy(session, row.copy_id).generation  # type: ignore[union-attr]
    (store / "o.dat").write_bytes(b"tampered")
    assert verify_file.fn(row.copy_id, generation=generation + 1) == FileState.AVAILABLE
    assert verify_file.fn(row.copy_id, generation=generation) == FileState.UNKNOWN


@pytest.mark.req("SVC-16")
def test_object_changing_while_hashed_is_left_as_it_is(
    ctx: AppContext, store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (store / "c.dat").write_bytes(b"first")
    with ctx.write_session() as session:
        result = models.register_file(
            session,
            project="demo",
            storage_location="primary",
            path="c.dat",
            hash=md5(b"first"),
            size=5,
        )
    real_md5 = StorageLocation.md5

    def growing(self: StorageLocation, path: str) -> str:
        digest = real_md5(self, path)
        (store / "c.dat").write_bytes(b"first, then more")
        return digest

    monkeypatch.setattr(StorageLocation, "md5", growing)
    assert verify_file.fn(result.copy_id) == FileState.PENDING
    assert copies(ctx)[0].state == FileState.PENDING
    monkeypatch.undo()
    assert verify_file.fn(result.copy_id) == FileState.UNKNOWN  # now stable, and different


@pytest.mark.req("SVC-16")
def test_size_mismatch_register_is_unknown(
    ctx: AppContext, client: TestClient, store: Path
) -> None:
    (store / "s.dat").write_bytes(b"12345")
    r = client.post(
        "/projects/demo/files", json={"path": "s.dat", "hash": md5(b"12345"), "size": 4}
    )
    assert r.status_code == 201
    assert r.json()["state"] == "unknown"
    assert r.json()["is_latest"] is False


@pytest.mark.req("SVC-11")
def test_accept_with_a_changed_generation_is_refused(
    ctx: AppContext, store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (store / "a.dat").write_bytes(b"as found")
    with ctx.write_session() as session:
        result = models.register_file(
            session,
            project="demo",
            storage_location="primary",
            path="a.dat",
            hash=md5(b"registered"),
            size=10,
        )
        models.set_copy_state(session, result.copy_id, FileState.UNKNOWN)

    def meanwhile() -> None:
        # Re-registered (generation bumped) and found unknown again: same
        # state, different revision.
        with ctx.write_session() as session:
            models.register_file(
                session,
                project="demo",
                storage_location="primary",
                path="a.dat",
                hash=md5(b"registered"),
                size=10,
            )
            models.set_copy_state(session, result.copy_id, FileState.UNKNOWN)

    _while_observing(monkeypatch, accept_mod, meanwhile)
    with pytest.raises(NotUnknownError, match="generation"):
        accept_copy.fn(result.file_id)
    (row,) = copies(ctx)
    assert (row.state, row.file_hash) == (FileState.UNKNOWN, md5(b"registered"))
    monkeypatch.undo()
    accept_copy.fn(result.file_id)
    (row,) = copies(ctx)
    assert (row.state, row.file_hash) == (FileState.AVAILABLE, md5(b"as found"))
    assert_consistent(ctx)


@pytest.mark.req("SVC-11")
def test_accept_refused_while_an_upload_owns_the_copy(
    ctx: AppContext, client: TestClient, store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(register_api.verify_file, "fn", lambda _id: None)
    rec = _upload(client, store, "b.dat", b"old")
    with ctx.write_session() as session:
        models.set_copy_state(session, copies(ctx)[0].copy_id, FileState.UNKNOWN)
    res = client.post(
        "/projects/demo/uploads", json={"path": "b.dat", "hash": md5(b"old"), "size": 3}
    ).json()
    assert res["mode"] == "new"
    r = client.post(f"/files/{rec['id']}/accept", json={"storage_location": "primary"})
    assert r.status_code == 409
    assert "pending" in r.json()["detail"]

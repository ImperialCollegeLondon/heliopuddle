"""Guarded delete: only what hp observed is removed; failures are reported (STO-13, SVC-15).

Deterministic interleavings inject the competing operation inside the delete's
store step, then assert invariants over the store and the DB.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from heliopuddle.server import models
from heliopuddle.server.api import create_api
from heliopuddle.server.api.files import register as register_api
from heliopuddle.server.context import AppContext
from heliopuddle.server.models import transitions as tr
from heliopuddle.server.models.snapshots import CopySnapshot
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services import delete as delete_mod
from heliopuddle.server.services.delete import delete_file
from heliopuddle.server.services.upload import RepairResult, repair_expired
from heliopuddle.storage import StorageLocation
from tests.lifecycle import assert_consistent, copies, md5

pytestmark = pytest.mark.usefixtures("inline_prefect")


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(tr, "clock", c)
    return c


@pytest.fixture
def client(ctx: AppContext) -> Iterator[TestClient]:
    with TestClient(create_api(ctx)) as c:
        yield c


def _upload(client: TestClient, store: Path, path: str, data: bytes) -> dict:
    res = client.post(
        "/projects/demo/uploads", json={"path": path, "hash": md5(data), "size": len(data)}
    ).json()
    if res["mode"] == "unchanged":
        return res["file"]
    (store / res["write_path"]).parent.mkdir(parents=True, exist_ok=True)
    (store / res["write_path"]).write_bytes(data)
    r = client.post(f"/uploads/{res['token']}/complete")
    assert r.status_code == 200, r.text
    return r.json()


def _before_store_step(monkeypatch: pytest.MonkeyPatch, hook: Any) -> None:
    """Run ``hook(snap)`` once, just before the first object is checked and deleted."""
    real = delete_mod.delete_object
    state = {"done": False}

    def wrapped(store: StorageLocation, snap: CopySnapshot, **kwargs: Any) -> FileState:
        if not state["done"]:
            state["done"] = True
            hook(snap)
        return real(store, snap, **kwargs)

    monkeypatch.setattr(delete_mod, "delete_object", wrapped)


@pytest.mark.req("STO-13")
def test_delete_removes_an_observed_object(
    ctx: AppContext, client: TestClient, store: Path
) -> None:
    rec = _upload(client, store, "a/x.dat", b"bytes")
    r = client.delete(f"/files/{rec['id']}")
    assert r.status_code == 200
    assert r.json()["deleted_at"] is not None
    assert r.json()["state"] == "deleted"
    assert not (store / "a/x.dat").exists()
    assert_consistent(ctx)


@pytest.mark.req("STO-13")
def test_object_changed_since_observed_is_left_unknown(
    ctx: AppContext, client: TestClient, store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another writer replaced the object after hp last saw it: the delete refuses it."""
    rec = _upload(client, store, "b.dat", b"observed")

    def meanwhile(snap: CopySnapshot) -> None:
        # The path is owned by the delete: an upload is busy...
        r = client.post(
            "/projects/demo/uploads", json={"path": "b.dat", "hash": md5(b"n"), "size": 1}
        )
        assert (r.status_code, r.json()["reason"]) == (409, "busy")
        # ...but a tool outside hp rewrites the object anyway.
        (store / "b.dat").write_bytes(b"newer, longer content")

    _before_store_step(monkeypatch, meanwhile)
    r = client.delete(f"/files/{rec['id']}")
    assert r.status_code == 200
    body = r.json()
    assert body["deleted_at"] is None
    assert body["state"] == "unknown"
    assert (store / "b.dat").read_bytes() == b"newer, longer content"
    assert copies(ctx)[0].token is None
    assert_consistent(ctx)


@pytest.mark.req("STO-13")
def test_stale_delete_never_removes_newer_content(
    ctx: AppContext,
    client: TestClient,
    store: Path,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Delete stalls past its lease; a repair finishes it; the path is re-uploaded; the
    stalled delete then resumes: the new bytes and record survive."""
    rec = _upload(client, store, "c.dat", b"version one")

    def meanwhile(snap: CopySnapshot) -> None:
        clock.now += timedelta(hours=2)
        assert repair_expired(snap.copy_id) == RepairResult.REPAIRED
        assert not (store / "c.dat").exists()
        _upload(client, store, "c.dat", b"version two, re-uploaded")

    _before_store_step(monkeypatch, meanwhile)
    assert delete_file.fn(rec["id"]) == FileState.DELETED  # the repair finished it

    assert (store / "c.dat").read_bytes() == b"version two, re-uploaded"
    new = client.get("/projects/demo/files/by-path", params={"path": "c.dat"}).json()
    assert new["id"] != rec["id"]
    assert (new["state"], new["hash"]) == ("available", md5(b"version two, re-uploaded"))
    assert client.get(f"/files/{rec['id']}").json()["deleted_at"] is not None
    assert_consistent(ctx)


@pytest.mark.req("STO-13")
def test_never_observed_copy_is_not_deleted_from_the_store(
    ctx: AppContext, client: TestClient, store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(register_api.verify_file, "fn", lambda _id: None)
    (store / "n.dat").write_bytes(b"never seen")
    r = client.post(
        "/projects/demo/files", json={"path": "n.dat", "hash": md5(b"never seen"), "size": 10}
    )
    assert r.json()["state"] == "pending"
    r = client.delete(f"/files/{r.json()['id']}")
    assert r.status_code == 200
    assert (r.json()["state"], r.json()["deleted_at"]) == ("unknown", None)
    assert (store / "n.dat").read_bytes() == b"never seen"


@pytest.mark.req("STO-13")
def test_absent_object_counts_as_deleted_and_native_md5_mismatch_refuses(
    ctx: AppContext, client: TestClient, store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone = _upload(client, store, "g.dat", b"gone")
    (store / "g.dat").unlink()
    assert client.delete(f"/files/{gone['id']}").json()["state"] == "deleted"

    kept = _upload(client, store, "k.dat", b"kept")
    monkeypatch.setattr(StorageLocation, "native_md5", lambda self, path: md5(b"other"))
    assert client.delete(f"/files/{kept['id']}").json()["state"] == "unknown"
    assert (store / "k.dat").exists()


@pytest.mark.req("SVC-15")
def test_store_error_is_200_deleting_and_repair_finishes(
    ctx: AppContext, client: TestClient, store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _upload(client, store, "e.dat", b"err")

    def broken(self: StorageLocation, path: str) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(StorageLocation, "rm", broken)
    r = client.delete(f"/files/{rec['id']}")
    assert r.status_code == 200
    assert (r.json()["state"], r.json()["deleted_at"]) == ("deleting", None)
    (row,) = copies(ctx)
    assert (row.state, row.token) == (FileState.DELETING, None)  # an unfinished delete
    assert (store / "e.dat").exists()
    # An upload to the path waits for that delete (it is retried first and fails again).
    r = client.post("/projects/demo/uploads", json={"path": "e.dat", "hash": "h", "size": 1})
    assert (r.status_code, r.json()["reason"]) == (409, "busy")

    monkeypatch.undo()
    assert repair_expired(row.copy_id) == RepairResult.REPAIRED
    assert not (store / "e.dat").exists()
    assert client.get(f"/files/{rec['id']}").json()["deleted_at"] is not None
    assert_consistent(ctx)


@pytest.mark.req("SVC-15")
def test_unexpected_delete_error_is_500(
    ctx: AppContext, client: TestClient, store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _upload(client, store, "u.dat", b"u")

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("database exploded")

    monkeypatch.setattr(models, "finish_delete", boom)
    r = client.delete(f"/files/{rec['id']}")
    assert r.status_code == 500
    assert "RuntimeError" in r.json()["detail"]
    assert "database exploded" not in r.text


@pytest.mark.req("STO-13")
def test_delete_repairs_an_expired_upload_on_a_copy_first(
    ctx: AppContext, client: TestClient, store: Path, clock: Clock
) -> None:
    rec = _upload(client, store, "r.dat", b"old")
    res = client.post(
        "/projects/demo/uploads",
        json={"path": "r.dat", "hash": md5(b"new"), "size": 3, "overwrite": True},
    ).json()
    (store / res["write_path"]).write_bytes(b"new")
    clock.now += timedelta(hours=2)
    assert delete_file.fn(rec["id"]) == FileState.DELETED
    assert list(store.iterdir()) == []
    assert_consistent(ctx)

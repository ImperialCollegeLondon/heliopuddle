"""The upload protocol through the API: reserve, write, renew, complete, abort (STO-09).

Deterministic phase interleavings: each test drives two "clients" step by step
and asserts invariants over both the store and the DB
(:func:`tests.lifecycle.assert_consistent`), not only the status codes.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from heliopuddle.server import models
from heliopuddle.server.api import create_api
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import File, FileStorageLocation
from heliopuddle.server.models import transitions as tr
from heliopuddle.server.models import uploads as uploads_db
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services.upload import RepairResult, repair_expired
from tests.lifecycle import assert_consistent, copies, md5, patch_upload

pytestmark = pytest.mark.req("STO-09")


class Clock:
    """A controllable clock for ownership decisions."""

    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(tr, "clock", c)
    return c


@pytest.fixture
def client(ctx: AppContext) -> Iterator[TestClient]:
    with TestClient(create_api(ctx), headers={"X-Forwarded-User": "alice"}) as c:
        yield c


def reserve(client: TestClient, path: str, data: bytes, **extra: Any) -> Any:
    return client.post(
        "/projects/demo/uploads",
        json={"path": path, "hash": md5(data), "size": len(data)} | extra,
    )


def write(store: Path, path: str, data: bytes) -> None:
    target = store / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def live_rows(ctx: AppContext, path: str) -> int:
    with ctx.sessionmaker() as session:
        return (
            session.scalar(
                select(func.count(File.id)).where(File.path == path, File.deleted_at.is_(None))
            )
            or 0
        )


def upload(client: TestClient, store: Path, path: str, data: bytes, **extra: Any) -> dict:
    res = reserve(client, path, data, **extra)
    assert res.status_code == 200, res.text
    body = res.json()
    if body["mode"] != "unchanged":
        write(store, body["write_path"], data)
        r = client.post(f"/uploads/{body['token']}/complete")
        assert r.status_code == 200, r.text
        return r.json()
    return body["file"]


def test_new_path_reserve_write_complete(ctx: AppContext, client: TestClient, store: Path) -> None:
    r = reserve(client, "a/x_v1.dat", b"one", metadata={"k": 1})
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["mode"] == "new"
    assert res["write_path"] == "a/x_v1.dat"
    assert res["token"] and res["deadline"]
    # Reserved, not yet published: pending with a token, not latest.
    assert res["file"]["state"] == "pending"
    assert res["file"]["is_latest"] is False
    (row,) = copies(ctx)
    assert row.token == res["token"]
    assert_consistent(ctx)

    write(store, "a/x_v1.dat", b"one")
    r = client.post(f"/uploads/{res['token']}/complete")
    assert r.status_code == 200, r.text
    rec = r.json()
    assert (rec["state"], rec["is_latest"], rec["hash"]) == ("available", True, md5(b"one"))
    assert rec["metadata"] == {"k": 1}
    assert rec["created_by"] == "alice"
    assert copies(ctx)[0].token is None
    assert_consistent(ctx)

    # Done: the token is no longer current.
    assert client.post(f"/uploads/{res['token']}/complete").status_code == 409
    assert client.post(f"/uploads/{res['token']}/renew").status_code == 409
    assert client.delete(f"/uploads/{res['token']}").status_code == 409

    # Same content again: unchanged, nothing to write.
    again = reserve(client, "a/x_v1.dat", b"one").json()
    assert (again["mode"], again["token"], again["write_path"]) == ("unchanged", None, None)
    assert again["file"]["id"] == rec["id"]


def test_complete_with_wrong_bytes_is_unknown(
    ctx: AppContext, client: TestClient, store: Path
) -> None:
    res = reserve(client, "w.dat", b"right").json()
    write(store, "w.dat", b"wrong!")
    rec = client.post(f"/uploads/{res['token']}/complete").json()
    assert rec["state"] == "unknown"
    assert rec["is_latest"] is False
    assert_consistent(ctx)


def test_conflict_and_validation(ctx: AppContext, client: TestClient, store: Path) -> None:
    first = upload(client, store, "c.dat", b"old")
    r = reserve(client, "c.dat", b"new")
    assert r.status_code == 409
    assert r.json()["file"]["id"] == first["id"]
    assert (store / "c.dat").read_bytes() == b"old"
    # Overwrite needs an available copy here; the secondary has none.
    r = reserve(client, "c.dat", b"new", overwrite=True, storage_location="secondary")
    assert r.status_code == 409
    assert "not available" in r.json()["detail"]
    assert reserve(client, "/abs", b"x").status_code == 422
    assert reserve(client, "x.dat", b"x", storage_location="nope").status_code == 404
    assert (
        client.post(
            "/projects/nope/uploads", json={"path": "a", "hash": "h", "size": 1}
        ).status_code
        == 404
    )
    assert_consistent(ctx)


def test_two_clients_on_an_absent_path(ctx: AppContext, client: TestClient, store: Path) -> None:
    """A reserves; B is busy (and gets nothing to write); A writes and completes."""
    a = reserve(client, "race.dat", b"A wins").json()
    b = reserve(client, "race.dat", b"B loses")
    assert b.status_code == 409
    body = b.json()
    assert body["reason"] == "busy"
    assert "write_path" not in body and "token" not in body
    assert 1 <= int(b.headers["Retry-After"]) <= 3600
    # The same content from B is busy too, and so is a register of it.
    assert reserve(client, "race.dat", b"A wins").status_code == 409
    r = client.post("/projects/demo/files", json={"path": "race.dat", "hash": md5(b"x"), "size": 1})
    assert (r.status_code, r.json()["reason"]) == (409, "busy")

    write(store, a["write_path"], b"A wins")
    assert client.post(f"/uploads/{a['token']}/complete").status_code == 200

    # B retries: a conflict, its bytes never written.
    retry = reserve(client, "race.dat", b"B loses")
    assert retry.status_code == 409
    assert retry.json()["file"]["hash"] == md5(b"A wins")
    assert (store / "race.dat").read_bytes() == b"A wins"
    assert live_rows(ctx, "race.dat") == 1
    assert_consistent(ctx)


def test_renew_extends_the_deadline(ctx: AppContext, client: TestClient, clock: Clock) -> None:
    res = reserve(client, "r.dat", b"r").json()
    clock.advance(timedelta(minutes=50))
    renewed = client.post(f"/uploads/{res['token']}/renew")
    assert renewed.status_code == 200
    assert renewed.json()["write_path"] == "r.dat"
    deadline = datetime.fromisoformat(renewed.json()["deadline"])
    assert deadline == clock.now + timedelta(hours=1)
    clock.advance(timedelta(minutes=50))
    # Still owned thanks to the renewal.
    assert reserve(client, "r.dat", b"r").status_code == 409


def test_expiry_and_takeover(
    ctx: AppContext, client: TestClient, store: Path, clock: Clock
) -> None:
    """A writes partial bytes and stalls; after expiry B takes over; A's token is dead."""
    a = reserve(client, "t.dat", b"A full content").json()
    write(store, "t.dat", b"A part")
    clock.advance(timedelta(hours=2))

    b = reserve(client, "t.dat", b"B content").json()
    assert b["mode"] == "new"
    # The partial object was removed and A's reservation (and file row) dropped.
    assert not (store / "t.dat").exists()
    assert live_rows(ctx, "t.dat") == 1
    assert b["file"]["hash"] == md5(b"B content")

    assert client.post(f"/uploads/{a['token']}/complete").status_code == 409
    assert client.post(f"/uploads/{a['token']}/renew").status_code == 409
    write(store, "t.dat", b"B content")
    rec = client.post(f"/uploads/{b['token']}/complete").json()
    assert (rec["state"], rec["hash"]) == ("available", md5(b"B content"))
    assert (store / "t.dat").read_bytes() == b"B content"
    assert_consistent(ctx)


def test_expired_upload_with_complete_bytes_is_kept(
    ctx: AppContext, client: TestClient, store: Path, clock: Clock
) -> None:
    """Content matching the reservation is never removed: it is verified instead.

    A's late complete is answered 200 with the record: the repair published exactly what A
    reserved (owner decision; it used to be 409).
    """
    a = reserve(client, "k.dat", b"kept").json()
    write(store, "k.dat", b"kept")
    clock.advance(timedelta(hours=2))
    b = reserve(client, "k.dat", b"kept").json()
    assert b["mode"] == "unchanged"
    assert b["file"]["state"] == "available"
    assert (store / "k.dat").read_bytes() == b"kept"
    late = client.post(f"/uploads/{a['token']}/complete")
    assert late.status_code == 200 and late.json()["hash"] == md5(b"kept")
    assert_consistent(ctx)


def test_crash_before_complete_is_repaired(
    ctx: AppContext, client: TestClient, store: Path, clock: Clock
) -> None:
    res = reserve(client, "crash.dat", b"full").json()
    write(store, "crash.dat", b"fu")
    with ctx.sessionmaker() as session:
        location_id = models.get_storage_location(session, "demo", "primary").id  # type: ignore[union-attr]
        assert uploads_db.find_expired(session, location_id, clock.now) == []
    copy_id = copies(ctx)[0].copy_id
    assert repair_expired(copy_id) == RepairResult.NOT_OWNED_OR_LIVE

    clock.advance(timedelta(hours=2))
    with ctx.sessionmaker() as session:
        assert uploads_db.find_expired(session, location_id, clock.now) == [copy_id]
    assert repair_expired(copy_id) == RepairResult.DROPPED
    assert copies(ctx) == []
    assert not (store / "crash.dat").exists()
    assert repair_expired(copy_id) == RepairResult.NOT_OWNED_OR_LIVE
    assert client.post(f"/uploads/{res['token']}/complete").status_code == 409
    assert_consistent(ctx)


def test_abort_new_upload_drops_it(ctx: AppContext, client: TestClient, store: Path) -> None:
    res = reserve(client, "ab.dat", b"abc").json()
    write(store, "ab.dat", b"a")
    assert client.delete(f"/uploads/{res['token']}").status_code == 204
    assert not (store / "ab.dat").exists()
    assert live_rows(ctx, "ab.dat") == 0
    assert client.get("/projects/demo/files/by-path", params={"path": "ab.dat"}).status_code == 404
    assert client.delete(f"/uploads/{res['token']}").status_code == 409
    assert_consistent(ctx)


def test_abort_on_an_existing_unknown_copy_leaves_its_object(
    ctx: AppContext, client: TestClient, store: Path
) -> None:
    rec = upload(client, store, "u.dat", b"good")
    write(store, "u.dat", b"corrupted")
    with ctx.write_session() as session:
        models.set_copy_state(session, copies(ctx)[0].copy_id, FileState.UNKNOWN)
    res = reserve(client, "u.dat", b"good").json()
    assert (res["mode"], res["file"]["id"]) == ("new", rec["id"])
    assert client.delete(f"/uploads/{res['token']}").status_code == 204
    # hp did not write it: the object stays, the copy is unknown, the file live.
    assert (store / "u.dat").read_bytes() == b"corrupted"
    assert copies(ctx)[0].state == FileState.UNKNOWN
    assert live_rows(ctx, "u.dat") == 1

    # Re-uploading the right content repairs it.
    again = upload(client, store, "u.dat", b"good")
    assert (again["id"], again["state"]) == (rec["id"], "available")
    assert_consistent(ctx)


def test_overwrite_publishes_and_marks_other_copies_unknown(
    ctx: AppContext, client: TestClient, store: Path, tmp_path: Path
) -> None:
    first = upload(client, store, "o/f_v3.dat", b"old", metadata={"a": 1})
    upload(client, tmp_path / "store2", "o/f_v3.dat", b"old", storage_location="secondary")
    when = "2020-01-01T00:00:00Z"
    res = reserve(
        client,
        "o/f_v3.dat",
        b"new!",
        overwrite=True,
        metadata={"b": 2},
        content_date=when,
    ).json()
    assert res["mode"] == "overwrite"
    assert res["write_path"] == f"o/f_v3.dat.{res['token']}.hp-tmp"
    # While staged, the old content is still served and latest.
    current = client.get(f"/files/{first['id']}").json()
    assert current["hash"] == md5(b"old")
    assert current["is_latest"] is True
    assert current["storage_locations"][0]["state"] == "overwriting"
    assert_consistent(ctx)

    write(store, res["write_path"], b"new!")
    r = client.post(f"/uploads/{res['token']}/complete", headers={"X-Forwarded-User": "bob"})
    assert r.status_code == 200, r.text
    rec = r.json()
    assert (rec["id"], rec["hash"], rec["size"], rec["version_number"]) == (
        first["id"],
        md5(b"new!"),
        4,
        3,
    )
    assert (rec["metadata"], rec["content_date"][:10]) == ({"b": 2}, "2020-01-01")
    assert rec["updated_by"] == "bob"
    assert [c["state"] for c in rec["storage_locations"]] == ["available", "unknown"]
    assert rec["is_latest"] is True
    assert (store / "o/f_v3.dat").read_bytes() == b"new!"
    assert_consistent(ctx)


def test_overwrite_with_mismatched_staging_keeps_the_original(
    ctx: AppContext, client: TestClient, store: Path
) -> None:
    upload(client, store, "m.dat", b"old")
    res = reserve(client, "m.dat", b"new", overwrite=True).json()
    write(store, res["write_path"], b"something else")
    r = client.post(f"/uploads/{res['token']}/complete")
    assert r.status_code == 409
    assert "original is kept" in r.json()["detail"]
    assert r.json()["file"]["storage_locations"][0]["state"] == "available"
    assert (store / "m.dat").read_bytes() == b"old"
    assert_consistent(ctx)


@pytest.mark.req("AUTH-07", "STO-10")
def test_publish_error_does_not_expose_backend_credentials(
    ctx: AppContext, client: TestClient, store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    upload(client, store, "private.dat", b"old")
    res = reserve(client, "private.dat", b"new", overwrite=True).json()
    write(store, res["write_path"], b"new")

    def broken(*args: Any, **kwargs: Any) -> None:
        raise OSError("backend://user:private-password@host/path?token=private-token")

    patch_upload(monkeypatch, "_move_into_place", broken)
    response = client.post(f"/uploads/{res['token']}/complete")
    assert response.status_code == 409
    assert "OSError" in response.json()["detail"]
    assert "original is kept" in response.json()["detail"]
    assert "private-password" not in response.text
    assert "private-token" not in response.text
    assert (store / "private.dat").read_bytes() == b"old"
    assert_consistent(ctx)


def test_abort_overwrite_removes_the_staged_file(
    ctx: AppContext, client: TestClient, store: Path
) -> None:
    upload(client, store, "s.dat", b"old")
    res = reserve(client, "s.dat", b"new", overwrite=True).json()
    write(store, res["write_path"], b"ne")
    assert client.delete(f"/uploads/{res['token']}").status_code == 204
    assert not (store / res["write_path"]).exists()
    assert (store / "s.dat").read_bytes() == b"old"
    assert copies(ctx)[0].state == FileState.AVAILABLE
    assert_consistent(ctx)


@pytest.mark.req("STO-10")
def test_overwrite_changes_file_fields_only_when_it_publishes(
    ctx: AppContext, client: TestClient, store: Path
) -> None:
    """An abandoned overwrite leaves updated_by, metadata and content date untouched."""
    first = upload(client, store, "m.dat", b"old", metadata={"v": 1})
    with TestClient(create_api(ctx), headers={"X-Forwarded-User": "carol"}) as carol:
        res = reserve(
            carol,
            "m.dat",
            b"new",
            overwrite=True,
            metadata={"v": 2},
            content_date="2026-01-02T00:00:00Z",
        ).json()
        write(store, res["write_path"], b"new")
        assert carol.delete(f"/uploads/{res['token']}").status_code == 204
        kept = client.get(f"/files/{first['id']}").json()
        assert (kept["metadata"], kept["content_date"], kept["updated_by"]) == (
            {"v": 1},
            first["content_date"],
            first["updated_by"],
        )

        res = reserve(carol, "m.dat", b"new", overwrite=True, metadata={"v": 2}).json()
        write(store, res["write_path"], b"new")
        published = carol.post(f"/uploads/{res['token']}/complete").json()
    assert (published["metadata"], published["updated_by"]) == ({"v": 2}, "carol")
    assert_consistent(ctx)


def test_delete_and_verify_wait_for_an_upload(
    ctx: AppContext, client: TestClient, store: Path
) -> None:
    rec = upload(client, store, "d.dat", b"old")
    res = reserve(client, "d.dat", b"new", overwrite=True).json()
    r = client.delete(f"/files/{rec['id']}")
    assert (r.status_code, r.json()["reason"]) == (409, "busy")
    assert int(r.headers["Retry-After"]) >= 1
    write(store, res["write_path"], b"new")
    assert client.post(f"/uploads/{res['token']}/complete").status_code == 200
    assert (store / "d.dat").read_bytes() == b"new"
    assert_consistent(ctx)


def test_register_repairs_an_expired_owner_first(
    ctx: AppContext, client: TestClient, store: Path, clock: Clock
) -> None:
    upload(client, store, "g.dat", b"old")
    res = reserve(client, "g.dat", b"new", overwrite=True).json()
    write(store, res["write_path"], b"new")
    clock.advance(timedelta(hours=2))
    # The overwrite was never completed: repair keeps the original and removes
    # the staged file, then the register sees the same content.
    r = client.post("/projects/demo/files", json={"path": "g.dat", "hash": md5(b"old"), "size": 3})
    assert r.status_code == 200, r.text
    assert (r.json()["hash"], r.json()["state"]) == (md5(b"old"), "available")
    assert not (store / res["write_path"]).exists()
    assert client.post(f"/uploads/{res['token']}/complete").status_code == 409
    assert_consistent(ctx)
    with ctx.sessionmaker() as session:
        assert session.scalar(select(func.count(FileStorageLocation.id))) == 1

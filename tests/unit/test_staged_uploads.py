# ruff: noqa: F811
"""Staged writes over existing objects, late completes, unobservable completes and the sweep.

STO-09 (ownership), STO-10 (publish, repair, sweep), STO-11 (staging names) and
docs/design/operations.md ("Add a file", "Expiry and repair"). Every test ends
with :func:`tests.adversarial.assert_invariants` over the store and the DB.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from heliopuddle.server.context import AppContext
from heliopuddle.server.models import uploads as uploads_db
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services.index import index_full
from heliopuddle.server.services.index_sweep import sweep_temp_file
from heliopuddle.server.services.upload import RepairResult, repair_expired
from heliopuddle.server.services.verify import verify_file
from heliopuddle.storage import StorageLocation
from heliopuddle.storage.location import backup_path, staging_path
from tests.adversarial import (
    Clock,
    assert_invariants,
    client,  # noqa: F401 - fixture
    clock,  # noqa: F401 - fixture
    config_dir,  # noqa: F401 - fixture
    protocol,  # noqa: F401 - fixture
    put,
    store,
)
from tests.lifecycle import copies, md5, patch_upload, store_bytes, temp_files, upload_attr

pytestmark = pytest.mark.usefixtures("inline_prefect")

OLD = b"original content"
NEW = b"the new content, longer"
NEWER = b"a third version of the content"
FOREIGN = b"an object hp never observed"
PATH = "p/data_v1.dat"


def reserve(client: TestClient, data: bytes, path: str = PATH, **extra: Any) -> Any:
    return client.post(
        "/projects/demo/uploads",
        json={"path": path, "hash": md5(data), "size": len(data)} | extra,
    )


def upload(
    client: TestClient, loc: StorageLocation, data: bytes, path: str = PATH, **extra: Any
) -> dict[str, Any]:
    res = reserve(client, data, path, **extra)
    assert res.status_code == 200, res.text
    body = res.json()
    if body["mode"] == "unchanged":
        return body["file"]
    put(loc, body["write_path"], data)
    done = client.post(f"/uploads/{body['token']}/complete")
    assert done.status_code == 200, done.text
    return done.json()


def only_copy(ctx: AppContext, location: str = "primary") -> Any:
    (row,) = [c for c in copies(ctx) if c.storage_location == location]
    return row


@pytest.fixture
def fast_index(ctx: AppContext) -> None:
    ctx.projects["demo"].index.settle_time = timedelta(0)


# --- never write directly over an existing object ---------------------------------------


@pytest.mark.req("STO-09")
def test_a_new_path_without_an_object_is_written_directly(
    ctx: AppContext, client: TestClient
) -> None:
    res = reserve(client, OLD).json()
    assert (res["mode"], res["write_path"]) == ("new", PATH)
    put(store(ctx), res["write_path"], OLD)
    assert client.post(f"/uploads/{res['token']}/complete").status_code == 200
    assert_invariants(ctx)


@pytest.mark.req("STO-10")
def test_an_object_hp_never_observed_is_staged_and_published(
    ctx: AppContext, client: TestClient
) -> None:
    loc = store(ctx)
    put(loc, PATH, FOREIGN)
    res = reserve(client, NEW).json()
    token = res["token"]
    assert (res["mode"], res["write_path"]) == ("new", staging_path(PATH, token))
    assert store_bytes(loc, PATH) == FOREIGN  # nothing was written over it

    put(loc, res["write_path"], NEW)
    done = client.post(f"/uploads/{token}/complete")

    assert done.status_code == 200, done.text
    assert done.json()["state"] == "available"
    assert store_bytes(loc, PATH) == NEW
    assert temp_files(loc) == []
    assert_invariants(ctx)


@pytest.mark.req("STO-10")
def test_reuploading_an_unknown_copy_is_staged_and_published(
    ctx: AppContext, client: TestClient
) -> None:
    loc = store(ctx)
    upload(client, loc, OLD)
    put(loc, PATH, FOREIGN)  # replaced behind hp's back
    verify_file.fn(only_copy(ctx).copy_id)
    assert only_copy(ctx).state == FileState.UNKNOWN

    res = reserve(client, OLD).json()
    assert res["write_path"] == staging_path(PATH, res["token"])
    assert store_bytes(loc, PATH) == FOREIGN
    put(loc, res["write_path"], OLD)
    assert client.post(f"/uploads/{res['token']}/complete").status_code == 200

    assert store_bytes(loc, PATH) == OLD
    assert only_copy(ctx).state == FileState.AVAILABLE
    assert temp_files(loc) == []
    assert_invariants(ctx)


@pytest.mark.req("STO-10")
def test_a_failed_staged_write_never_truncates_the_existing_object(
    ctx: AppContext, client: TestClient
) -> None:
    loc = store(ctx)
    put(loc, PATH, FOREIGN)
    res = reserve(client, NEW).json()
    put(loc, res["write_path"], NEW[:4])  # a partial write

    done = client.post(f"/uploads/{res['token']}/complete")

    assert done.status_code == 409
    assert store_bytes(loc, PATH) == FOREIGN
    assert temp_files(loc) == []
    assert copies(ctx) == []  # the reservation (and its file row) was dropped
    assert_invariants(ctx)


@pytest.mark.req("STO-10")
def test_aborting_a_staged_reservation_removes_only_the_staged_file(
    ctx: AppContext, client: TestClient
) -> None:
    loc = store(ctx)
    put(loc, PATH, FOREIGN)
    res = reserve(client, NEW).json()
    put(loc, res["write_path"], NEW)

    assert client.delete(f"/uploads/{res['token']}").status_code == 204

    assert store_bytes(loc, PATH) == FOREIGN
    assert temp_files(loc) == []
    assert_invariants(ctx)


@pytest.mark.req("STO-10")
def test_repair_of_a_staged_reservation_removes_only_the_staged_file(
    ctx: AppContext, client: TestClient, clock: Clock
) -> None:
    loc = store(ctx)
    put(loc, PATH, FOREIGN)
    res = reserve(client, NEW).json()
    put(loc, res["write_path"], NEW[:4])
    clock.advance(timedelta(hours=2))

    assert repair_expired(only_copy(ctx).copy_id) == RepairResult.DROPPED

    assert store_bytes(loc, PATH) == FOREIGN
    assert temp_files(loc) == []
    assert_invariants(ctx)


@pytest.mark.req("STO-10")
def test_repair_publishes_a_complete_staged_file_of_an_expired_reservation(
    ctx: AppContext, client: TestClient, clock: Clock
) -> None:
    loc = store(ctx)
    put(loc, PATH, FOREIGN)
    res = reserve(client, NEW).json()
    put(loc, res["write_path"], NEW)
    clock.advance(timedelta(hours=2))

    assert repair_expired(only_copy(ctx).copy_id) == RepairResult.REPAIRED

    assert store_bytes(loc, PATH) == NEW
    assert only_copy(ctx).state == FileState.AVAILABLE
    assert temp_files(loc) == []
    assert_invariants(ctx)


@pytest.mark.req("STO-10")
def test_repair_restores_the_existing_object_when_a_publish_was_cut_short(
    ctx: AppContext, client: TestClient, clock: Clock, protocol: str
) -> None:
    """The existing object was moved to its backup and the process died before the staged move."""
    loc = store(ctx)
    put(loc, PATH, FOREIGN)
    res = reserve(client, NEW).json()
    token = res["token"]
    put(loc, res["write_path"], b"incomplete")  # staged content that does not match
    loc.move(PATH, backup_path(PATH, token))
    clock.advance(timedelta(hours=2))

    repair_expired(only_copy(ctx).copy_id)

    assert store_bytes(loc, PATH) == FOREIGN
    assert temp_files(loc) == []
    assert_invariants(ctx)


# --- a late complete after the repair published the same content ------------------------


@pytest.mark.req("STO-09")
@pytest.mark.parametrize("kind", ["new", "overwrite"])
def test_late_complete_after_a_repair_published_the_same_content_is_200(
    ctx: AppContext, client: TestClient, clock: Clock, kind: str
) -> None:
    loc = store(ctx)
    if kind == "overwrite":
        upload(client, loc, OLD)
    data = NEW if kind == "overwrite" else OLD
    res = reserve(client, data, overwrite=kind == "overwrite").json()
    # An overwrite whose move into place happened but whose DB step never did.
    put(loc, PATH if kind == "overwrite" else res["write_path"], data)
    clock.advance(timedelta(hours=2))
    assert repair_expired(only_copy(ctx).copy_id) == RepairResult.REPAIRED

    late = client.post(f"/uploads/{res['token']}/complete")

    assert late.status_code == 200, late.text
    assert late.json()["hash"] == md5(data)
    assert only_copy(ctx).state == FileState.AVAILABLE
    assert store_bytes(loc, PATH) == data
    assert_invariants(ctx)


@pytest.mark.req("STO-09")
def test_late_complete_is_refused_once_the_content_changed_again(
    ctx: AppContext, client: TestClient, clock: Clock
) -> None:
    loc = store(ctx)
    upload(client, loc, OLD)
    res = reserve(client, NEW, overwrite=True).json()
    put(loc, res["write_path"], NEW)
    clock.advance(timedelta(hours=2))
    assert repair_expired(only_copy(ctx).copy_id) == RepairResult.REPAIRED
    upload(client, loc, NEWER, overwrite=True)  # a newer publish

    assert client.post(f"/uploads/{res['token']}/complete").status_code == 409
    assert store_bytes(loc, PATH) == NEWER
    assert_invariants(ctx)


@pytest.mark.req("STO-09")
def test_late_complete_is_refused_when_the_repair_did_not_publish(
    ctx: AppContext, client: TestClient, clock: Clock
) -> None:
    loc = store(ctx)
    res = reserve(client, OLD).json()
    put(loc, res["write_path"], OLD[:3])
    clock.advance(timedelta(hours=2))
    repair_expired(only_copy(ctx).copy_id)

    assert client.post(f"/uploads/{res['token']}/complete").status_code == 409
    assert_invariants(ctx)


# --- complete when the object cannot be observed ---------------------------------------


@pytest.mark.req("STO-09")
def test_complete_that_cannot_read_the_object_keeps_the_reservation(
    ctx: AppContext, client: TestClient, monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    loc = store(ctx)
    res = reserve(client, OLD).json()
    put(loc, res["write_path"], OLD)
    before = only_copy(ctx)
    with ctx.sessionmaker() as session:
        generation = uploads_db.snapshot(session, before.copy_id).generation  # type: ignore[union-attr]
    real = upload_attr("observe")
    calls: list[int] = []

    def flaky(store_: StorageLocation, path: str) -> Any:
        if not calls:
            calls.append(1)
            raise OSError("injected: transient read error")
        return real(store_, path)

    patch_upload(monkeypatch, "observe", flaky)
    first = client.post(f"/uploads/{res['token']}/complete")

    assert first.status_code == 503, first.text
    kept = only_copy(ctx)
    assert (kept.state, kept.token) == (FileState.PENDING, res["token"])  # still the client's
    with ctx.sessionmaker() as session:
        snap = uploads_db.snapshot(session, kept.copy_id)
    assert snap is not None and snap.generation == generation
    assert_invariants(ctx, allow_temp=True)

    retry = client.post(f"/uploads/{res['token']}/complete")
    assert retry.status_code == 200, retry.text
    assert only_copy(ctx).state == FileState.AVAILABLE
    assert_invariants(ctx)


@pytest.mark.req("STO-09")
def test_an_unobserved_complete_that_is_never_retried_is_settled_by_repair(
    ctx: AppContext, client: TestClient, monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    loc = store(ctx)
    res = reserve(client, OLD).json()
    put(loc, res["write_path"], OLD)
    real = upload_attr("observe")
    calls: list[int] = []

    def flaky(store_: StorageLocation, path: str) -> Any:
        if not calls:
            calls.append(1)
            raise OSError("injected")
        return real(store_, path)

    patch_upload(monkeypatch, "observe", flaky)
    assert client.post(f"/uploads/{res['token']}/complete").status_code == 503
    clock.advance(timedelta(hours=2))

    assert repair_expired(only_copy(ctx).copy_id) == RepairResult.REPAIRED
    assert only_copy(ctx).state == FileState.AVAILABLE
    assert_invariants(ctx)


# --- the owner of another location's copy settles it itself ------------------------------


def _two_location_file(client: TestClient, ctx: AppContext) -> None:
    for name in ("primary", "secondary"):
        upload(client, store(ctx, name), OLD, storage_location=name)


@pytest.mark.req("STO-09")
def test_an_owned_copy_publishes_after_another_location_published(
    ctx: AppContext, client: TestClient
) -> None:
    _two_location_file(client, ctx)
    b = reserve(client, NEW, overwrite=True, storage_location="secondary").json()
    put(store(ctx, "secondary"), b["write_path"], NEW)
    upload(client, store(ctx), NEWER, overwrite=True)  # A publishes at the primary

    done = client.post(f"/uploads/{b['token']}/complete")  # B publishes last

    assert done.status_code == 200, done.text
    assert only_copy(ctx, "secondary").state == FileState.AVAILABLE
    assert only_copy(ctx, "primary").state == FileState.UNKNOWN  # now holds the old content
    assert_invariants(ctx)


@pytest.mark.req("STO-09")
def test_an_owned_copy_that_aborts_after_another_location_published_is_unknown(
    ctx: AppContext, client: TestClient
) -> None:
    _two_location_file(client, ctx)
    b = reserve(client, NEW, overwrite=True, storage_location="secondary").json()
    put(store(ctx, "secondary"), b["write_path"], NEW)
    upload(client, store(ctx), NEWER, overwrite=True)

    assert client.delete(f"/uploads/{b['token']}").status_code == 204

    assert only_copy(ctx, "secondary").state == FileState.UNKNOWN  # holds OLD, file is NEWER
    assert only_copy(ctx, "primary").state == FileState.AVAILABLE
    assert_invariants(ctx)


# --- the sweep ---------------------------------------------------------------------------


@pytest.mark.req("STO-10")
def test_full_index_counts_swept_files_and_leaves_owned_ones(
    ctx: AppContext, client: TestClient, fast_index: None
) -> None:
    loc = store(ctx)
    upload(client, loc, OLD, "p/data.dat")
    upload(client, loc, OLD, "p/data.dat.v2")
    live = reserve(client, NEW, "p/data.dat.v2", overwrite=True).json()
    put(loc, live["write_path"], NEW)  # owned: a live overwrite of the longer sibling path
    orphan = staging_path("p/data.dat", "c09d35fb-afcb-4f15-b2c2-d2d41e2a1bfb")
    put(loc, orphan, b"zombie")

    summary = index_full.fn("demo", "primary")

    assert summary.swept == 1
    assert store_bytes(loc, orphan) is None
    assert store_bytes(loc, live["write_path"]) == NEW  # the sibling's staging file survives
    done = client.post(f"/uploads/{live['token']}/complete")
    assert done.status_code == 200, done.text
    assert_invariants(ctx)


@pytest.mark.req("STO-10")
def test_sweep_restores_a_backup_when_the_final_object_is_missing(
    ctx: AppContext, client: TestClient, fast_index: None
) -> None:
    loc = store(ctx)
    upload(client, loc, OLD)
    token = "c09d35fb-afcb-4f15-b2c2-d2d41e2a1bfb"
    loc.move(PATH, backup_path(PATH, token))  # a publish died between its two moves

    summary = index_full.fn("demo", "primary")

    assert summary.swept == 1
    assert store_bytes(loc, PATH) == OLD
    assert temp_files(loc) == []
    assert_invariants(ctx)


@pytest.mark.req("STO-10")
def test_sweep_restores_a_backup_when_the_final_object_does_not_match(
    ctx: AppContext, client: TestClient
) -> None:
    loc = store(ctx)
    upload(client, loc, OLD)
    token = "c09d35fb-afcb-4f15-b2c2-d2d41e2a1bfb"
    backup = backup_path(PATH, token)
    put(loc, backup, OLD)
    put(loc, PATH, FOREIGN)  # the recorded hash is OLD's
    project_id, location_id = _ids(ctx)

    outcome = sweep_temp_file(
        ctx, loc, project_id=project_id, storage_location_id=location_id, path=backup
    )

    assert outcome == "restored"
    assert store_bytes(loc, PATH) == OLD
    assert temp_files(loc) == []


@pytest.mark.req("STO-10")
def test_sweep_removes_a_backup_when_the_final_object_matches(
    ctx: AppContext, client: TestClient
) -> None:
    loc = store(ctx)
    upload(client, loc, OLD)
    backup = backup_path(PATH, "c09d35fb-afcb-4f15-b2c2-d2d41e2a1bfb")
    put(loc, backup, b"older")
    project_id, location_id = _ids(ctx)

    outcome = sweep_temp_file(
        ctx, loc, project_id=project_id, storage_location_id=location_id, path=backup
    )

    assert outcome == "removed"
    assert store_bytes(loc, PATH) == OLD
    assert temp_files(loc) == []


@pytest.mark.req("STO-10")
def test_sweep_leaves_files_that_are_not_hp_temporary_names(
    ctx: AppContext, client: TestClient
) -> None:
    loc = store(ctx)
    upload(client, loc, OLD)
    put(loc, "p/.data_v1.dat.abc123.hp-tmp", b"a client's own temporary file")
    project_id, location_id = _ids(ctx)

    outcome = sweep_temp_file(
        ctx,
        loc,
        project_id=project_id,
        storage_location_id=location_id,
        path="p/.data_v1.dat.abc123.hp-tmp",
    )

    assert outcome == "left"
    assert store_bytes(loc, "p/.data_v1.dat.abc123.hp-tmp") is not None


def _ids(ctx: AppContext) -> tuple[int, int]:
    from heliopuddle.server import models

    with ctx.sessionmaker() as session:
        row = models.get_storage_location(session, "demo", "primary")
        assert row is not None
        return row.project_id, row.id

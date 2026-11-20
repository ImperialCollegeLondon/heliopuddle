"""Adversarial uploads: crash points, other copies and publish faults."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from heliopuddle.server.context import AppContext
from heliopuddle.server.models import uploads as uploads_db
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services.upload import RepairResult, repair_expired
from heliopuddle.storage import StorageLocation
from heliopuddle.storage.location import backup_path, staging_path
from tests.adversarial import (
    Clock,
    assert_invariants,
    client,
    clock,
    config_dir,  # noqa: F401 - fixture
    protocol,
    put,
    store,
)
from tests.lifecycle import copies, md5, patch_upload, store_bytes, temp_files, upload_attr
from tests.unit._adversarial_uploads_helpers import (
    NEW,
    NEWER,
    OLD,
    PATH,
    only_copy,
    reserve,
    upload,
)

_FIXTURES = (
    client,
    clock,
    protocol,
)  # referenced so ruff sees the imports used

# --- crash points between store I/O and the DB commit ----------------------------------


@pytest.mark.req("STO-09")
def test_crash_between_the_object_check_and_the_db_commit_of_a_new_upload(
    ctx: AppContext, client: TestClient, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """finish_new raises (a crash after the store was observed): the next repair records it."""
    loc = store(ctx)
    a = reserve(client, OLD).json()
    put(loc, a["write_path"], OLD)
    real = uploads_db.finish_new
    calls = {"n": 0}

    def crash(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("crash")
        return real(*args, **kwargs)

    monkeypatch.setattr(uploads_db, "finish_new", crash)
    with pytest.raises(RuntimeError):
        client.post(f"/uploads/{a['token']}/complete")
    clock.advance(timedelta(seconds=1))
    assert repair_expired(only_copy(ctx).copy_id) == RepairResult.REPAIRED
    row = only_copy(ctx)
    assert (row.state, row.token, row.file_hash) == (FileState.AVAILABLE, None, md5(OLD))
    assert store_bytes(loc, PATH) == OLD
    assert_invariants(ctx)


# --- ownership of the other copies of a file (STO-09) -----------------------------------


def _two_location_file(client: TestClient, ctx: AppContext, data: bytes) -> None:
    for location in ("primary", "secondary"):
        upload(client, store(ctx, location), data, storage_location=location)


@pytest.mark.req("STO-09")
def test_publishing_at_one_location_does_not_clobber_an_overwrite_owned_at_another(
    ctx: AppContext, client: TestClient, clock: Clock
) -> None:
    """One file, two storage locations. B (secondary) has reserved an overwrite and owns its copy.

    A overwrites at the primary and publishes. ``finish_overwrite`` turns "the
    file's other copies" ``unknown`` without looking at their tokens, so B's
    copy is ``unknown`` while still holding B's live token: ownership outlived
    its state. Invariant: a token-holding copy is in an owned state, and an
    expired one is repairable (B's copy must not stay wedged forever).
    """
    _two_location_file(client, ctx, OLD)
    b = reserve(client, NEW, overwrite=True, storage_location="secondary").json()
    put(store(ctx, "secondary"), b["write_path"], NEW)
    upload(client, store(ctx), NEWER, overwrite=True)  # A publishes at the primary

    assert_invariants(ctx, allow_temp=True)

    clock.advance(timedelta(hours=2))
    secondary = only_copy(ctx, "secondary")
    assert repair_expired(secondary.copy_id) != RepairResult.INCOMPLETE
    again = reserve(client, NEWER, storage_location="secondary")
    assert again.status_code == 200, again.text  # the copy is not wedged by a dead token


@pytest.mark.req("STO-09")
def test_accepting_a_copy_does_not_clobber_an_overwrite_owned_at_another_location(
    ctx: AppContext, client: TestClient, clock: Clock
) -> None:
    """Accept at the secondary (different content) while the primary owns an overwrite.

    Same invariant as above through ``models.accept.accept_copy_record``.
    """
    _two_location_file(client, ctx, OLD)
    put(store(ctx, "secondary"), PATH, NEWER)  # someone replaced it behind hp's back
    from heliopuddle.server.services.verify import verify_file

    verify_file.fn(only_copy(ctx, "secondary").copy_id)
    assert only_copy(ctx, "secondary").state == FileState.UNKNOWN
    a = reserve(client, NEW, overwrite=True).json()  # primary owns an overwrite
    put(store(ctx), a["write_path"], NEW)

    r = client.post("/files/1/accept", json={"storage_location": "secondary"})
    assert r.status_code == 200, r.text

    assert_invariants(ctx, allow_temp=True)
    clock.advance(timedelta(hours=2))
    primary = only_copy(ctx, "primary")
    assert repair_expired(primary.copy_id) != RepairResult.INCOMPLETE


# --- overwrite publish faults, then a stale second repair -------------------------------


@pytest.mark.req("STO-10")
def test_repair_is_idempotent_and_leaves_no_temporary_files(
    ctx: AppContext, client: TestClient, clock: Clock, protocol: str
) -> None:
    """Abandon an overwrite with a staged file and a stray backup; repair twice."""
    loc = store(ctx)
    upload(client, loc, OLD)
    a = reserve(client, NEW, overwrite=True).json()
    put(loc, a["write_path"], NEW)
    put(loc, backup_path(PATH, a["token"]), OLD)
    clock.advance(timedelta(hours=2))
    copy_id = only_copy(ctx).copy_id
    assert repair_expired(copy_id) == RepairResult.REPAIRED
    assert repair_expired(copy_id) == RepairResult.NOT_OWNED_OR_LIVE
    assert temp_files(loc) == []
    assert store_bytes(loc, PATH) == OLD
    assert_invariants(ctx)


@pytest.mark.req("STO-10")
def test_staging_path_is_unique_per_reservation(
    ctx: AppContext, client: TestClient, clock: Clock
) -> None:
    """Two successive overwrite reservations never share a staging path (STO-11)."""
    loc = store(ctx)
    upload(client, loc, OLD)
    a = reserve(client, NEW, overwrite=True).json()
    clock.advance(timedelta(hours=2))
    assert repair_expired(only_copy(ctx).copy_id) == RepairResult.REPAIRED
    b = reserve(client, NEWER, overwrite=True).json()
    assert a["write_path"] != b["write_path"]
    assert a["write_path"] == staging_path(PATH, a["token"])


# --- repair must only touch the temporary files of its own path (STO-11) ----------------


@pytest.mark.req("STO-11")
def test_repair_of_one_path_does_not_remove_the_staging_file_of_a_longer_sibling_path(
    ctx: AppContext, client: TestClient, clock: Clock
) -> None:
    """``temp_files`` finds a path's staging files by the name prefix ``<basename>.``.

    ``p/data.dat`` and ``p/data.dat.v2`` are different paths, but the staging
    file of the second (``p/data.dat.v2.<token>.hp-tmp``) starts with the
    first's prefix. An overwrite of ``p/data.dat`` expires and is repaired
    while an overwrite of ``p/data.dat.v2`` is live: the repair treats the live
    operation's staging file as its own abandoned one and removes it. STO-11:
    "only its owner cleans it up". The live overwrite must still publish.
    """
    loc = store(ctx)
    short, longer = "p/data.dat", "p/data.dat.v2"
    upload(client, loc, OLD, short)
    upload(client, loc, OLD, longer)
    reserve(client, NEW, short, overwrite=True)  # will expire
    clock.advance(timedelta(minutes=50))
    b = reserve(client, NEWER, longer, overwrite=True).json()  # still live
    put(loc, b["write_path"], NEWER)
    clock.advance(timedelta(minutes=20))
    short_copy = next(c for c in copies(ctx) if c.path == short)

    assert repair_expired(short_copy.copy_id) == RepairResult.REPAIRED

    assert store_bytes(loc, b["write_path"]) == NEWER, "another path's staging file was removed"
    done = client.post(f"/uploads/{b['token']}/complete")
    assert done.status_code == 200, done.text
    assert store_bytes(loc, longer) == NEWER
    assert_invariants(ctx)


# --- faults that act and then raise (an ambiguous store answer) --------------------------


@pytest.mark.req("STO-10")
@pytest.mark.parametrize("where", ["backup", "publish"])
def test_a_store_move_that_happened_but_reported_failure_is_settled_from_the_store(
    ctx: AppContext,
    client: TestClient,
    clock: Clock,
    protocol: str,
    where: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The move is performed and then the call raises (a timeout after the server acted).

    The copy must be settled from what the store holds: the original kept when
    only the backup move happened, the new content recorded when the publish
    move happened. Either way no version is lost and the DB agrees.
    """
    if where == "backup" and protocol == "memory":
        pytest.skip("a backup move exists only where a move does not replace")
    loc = store(ctx)
    upload(client, loc, OLD)
    a = reserve(client, NEW, overwrite=True).json()
    put(loc, a["write_path"], NEW)
    real_mv = upload_attr("_mv")  # pyright: ignore[reportPrivateUsage]
    staged = staging_path(PATH, a["token"])

    def mv(store_: StorageLocation, src: str, dst: str) -> None:
        real_mv(store_, src, dst)
        if (where == "backup" and dst == backup_path(PATH, a["token"])) or (
            where == "publish" and (src, dst) == (staged, PATH)
        ):
            raise TimeoutError("injected: the move happened but the answer was lost")

    patch_upload(monkeypatch, "_mv", mv)
    r = client.post(f"/uploads/{a['token']}/complete")

    expected = NEW if where == "publish" else OLD
    assert r.status_code == (200 if where == "publish" else 409), r.text
    assert store_bytes(loc, PATH) == expected
    (row,) = copies(ctx)
    assert (row.state, row.token, row.file_hash) == (FileState.AVAILABLE, None, md5(expected))
    assert_invariants(ctx)

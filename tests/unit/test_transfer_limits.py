"""Transfer service: space limits, ownership and takeovers."""

from __future__ import annotations

import threading
from datetime import timedelta
from typing import Any

import pytest
from pydantic import ByteSize

from heliopuddle.config.models import RuleOptions
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import FileStorageLocation
from heliopuddle.server.models import transitions as tr
from heliopuddle.server.models import uploads
from heliopuddle.server.models.snapshots import CopySnapshot
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services import transfer
from heliopuddle.server.services.upload import (
    RepairResult,
    complete_upload,
    repair_expired,
)
from heliopuddle.server.services.verify import verify_file
from heliopuddle.storage import StorageLocation, backends
from tests.adversarial import (
    Clock,
    assert_invariants,
    clock,
    config_dir,  # noqa: F401 - fixture
    protocol,  # noqa: F401 - fixture
    put,
    store,
)
from tests.lifecycle import md5, store_bytes, temp_files
from tests.unit._transfer_helpers import (  # noqa: F401
    DATA,
    OTHER,
    PATH,
    copy,
    limits,
    upload,
)

_FIXTURES = (clock,)  # referenced so ruff sees the imports used


@pytest.mark.req("STO-04", "STO-14")
@pytest.mark.parametrize("limiting", ["native", "quota", "neither"])
@pytest.mark.parametrize("short", [False, True])
def test_space_uses_non_deleted_indexed_sizes_and_native_minimum(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch, limiting: str, short: bool
) -> None:
    file_id = upload(ctx)
    upload(ctx, b"x" * 40, "secondary", "used.dat")
    gone = upload(ctx, b"x" * 100, "secondary", "deleted.dat")
    transfer.delete_source_copy.fn(gone, "secondary")
    overhead = 10
    needed = len(DATA) + overhead
    native = needed - int(short) if limiting == "native" else None
    quota = 40 + needed - int(short) if limiting == "quota" else None
    cfg = ctx.projects["demo"].storage_location("secondary")
    cfg.quota = ByteSize(quota) if quota is not None else None
    monkeypatch.setattr(backends, "free_space", lambda *_args: native)
    original = copy(ctx)
    options = RuleOptions(check_for_space_overhead=ByteSize(overhead))
    if limiting != "neither" and short:
        with pytest.raises(transfer.InsufficientSpace, match="source plus overhead"):
            transfer.transfer_file.fn(
                file_id, "primary", "secondary", action="move", options=options
            )
        assert copy(ctx) == original
        assert not store(ctx, "secondary").exists(PATH)
    else:
        assert (
            transfer.transfer_file.fn(file_id, "primary", "secondary", options=options).outcome
            == "copied"
        )
    assert_invariants(ctx)


@pytest.mark.req("STO-04")
def test_default_overhead_is_enforced_and_space_checks_can_be_disabled(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_id = upload(ctx)
    monkeypatch.setattr(backends, "free_space", lambda *_args: 2_000_000_000 + len(DATA) - 1)
    with pytest.raises(transfer.InsufficientSpace):
        transfer.transfer_file.fn(file_id, "primary", "secondary")

    def unexpected(*_args: Any) -> Any:
        raise AssertionError("disabled space check ran")

    monkeypatch.setattr(backends, "free_space", unexpected)
    ctx.projects["demo"].storage_location("secondary").quota = ByteSize(1)
    assert (
        transfer.transfer_file.fn(
            file_id, "primary", "secondary", options=RuleOptions(check_for_space=False)
        ).outcome
        == "copied"
    )
    assert_invariants(ctx)


@pytest.mark.req("SVC-19", "STO-03")
@pytest.mark.parametrize("endpoint", ["primary", "secondary"])
def test_url_change_during_transfer_never_deletes_source_or_touches_new_root(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    file_id = upload(ctx)
    original_source = store(ctx)
    real_observe = transfer.observe
    changed = False

    def observe(loc: StorageLocation, path: str) -> Any:
        nonlocal changed
        obs = real_observe(loc, path)
        if loc.url == store(ctx, "secondary").url and obs is not None and not changed:
            changed = True
            cfg = ctx.projects["demo"].storage_location(endpoint)
            cfg.url += "-moved"
            put(store(ctx, endpoint), PATH, OTHER)
            ctx.sync_config()
        return obs

    monkeypatch.setattr(transfer, "observe", observe)
    with pytest.raises(tr.StaleOperation):
        transfer.transfer_file.fn(file_id, "primary", "secondary", action="move")
    assert store_bytes(original_source, PATH) == DATA
    assert store_bytes(store(ctx, endpoint), PATH) == OTHER
    assert copy(ctx, endpoint).token is None
    assert copy(ctx, endpoint).state == FileState.PENDING
    assert_invariants(ctx)


@pytest.mark.req("SVC-19")
def test_upload_delete_verify_and_index_cannot_take_a_live_transfer_destination(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    file_id = upload(ctx)
    original_source = copy(ctx)
    real_copy = transfer._copy_bytes

    def during_copy(*args: Any, **kwargs: Any) -> None:
        destination = copy(ctx, "secondary")
        assert destination.token is not None
        assert copy(ctx) == original_source
        with pytest.raises(tr.CopyBusy), ctx.write_session() as session:
            uploads.reserve(
                session,
                project="demo",
                storage_location="secondary",
                path=PATH,
                hash=md5(OTHER),
                size=len(OTHER),
                lease=ctx.settings.op_lease,
                overwrite=True,
            )
        with pytest.raises(tr.CopyBusy):
            transfer.delete.delete_file.fn(file_id)
        verify_file.fn(destination.copy_id)
        assert copy(ctx, "secondary") == destination
        assert repair_expired(destination.copy_id, now=clock()) == RepairResult.NOT_OWNED_OR_LIVE
        real_copy(*args, **kwargs)

    monkeypatch.setattr(transfer, "_copy_bytes", during_copy)
    assert transfer.transfer_file.fn(file_id, "primary", "secondary").outcome == "copied"
    assert_invariants(ctx)


@pytest.mark.req("SVC-19", "STO-03")
def test_expired_transfer_taken_over_during_verification_fails_without_deleting_source(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    file_id = upload(ctx)
    original = copy(ctx)
    real_observe = transfer.observe
    repaired = False

    def observe(loc: StorageLocation, path: str) -> Any:
        nonlocal repaired
        obs = real_observe(loc, path)
        if loc.url == store(ctx, "secondary").url and obs is not None and not repaired:
            repaired = True
            clock.advance(timedelta(hours=2))
            assert repair_expired(copy(ctx, "secondary").copy_id) == RepairResult.REPAIRED
        return obs

    monkeypatch.setattr(transfer, "observe", observe)
    with pytest.raises(tr.StaleOperation):
        transfer.transfer_file.fn(file_id, "primary", "secondary", action="move")
    assert copy(ctx) == original
    assert copy(ctx, "secondary").state == FileState.AVAILABLE
    assert copy(ctx, "secondary").token is None
    assert_invariants(ctx)


@pytest.mark.req("SVC-19")
def test_stale_staged_transfer_repair_does_not_replace_the_original(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_id = upload(ctx)
    put(store(ctx, "secondary"), PATH, OTHER)
    real_publish = transfer._move_into_place

    def stopped(*_args: Any) -> None:
        raise OSError("worker stopped before publish")

    monkeypatch.setattr(transfer, "_move_into_place", stopped)
    with pytest.raises(OSError, match="worker stopped"):
        transfer.transfer_file.fn(
            file_id, "primary", "secondary", options=RuleOptions(overwrite=True)
        )
    monkeypatch.setattr(transfer, "_move_into_place", real_publish)
    upload(ctx, b"new logical content", overwrite=True)
    assert repair_expired(copy(ctx, "secondary").copy_id) == RepairResult.DROPPED
    assert store_bytes(store(ctx, "secondary"), PATH) == OTHER
    assert store_bytes(store(ctx), PATH) == b"new logical content"
    assert temp_files(store(ctx, "secondary")) == []
    assert_invariants(ctx)


@pytest.mark.req("STO-03", "SVC-20")
def test_source_changed_without_a_new_generation_is_rehashed_before_delete(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_id = upload(ctx)
    original = copy(ctx)
    real_delete = transfer._delete_source

    def replace_before_delete(*args: Any, **kwargs: Any) -> Any:
        put(store(ctx), PATH, b"x" * len(DATA))
        return real_delete(*args, **kwargs)

    monkeypatch.setattr(transfer, "_delete_source", replace_before_delete)
    with pytest.raises(tr.StaleOperation, match="source content"):
        transfer.transfer_file.fn(file_id, "primary", "secondary", action="move")
    assert copy(ctx) == original
    assert store_bytes(store(ctx), PATH) == b"x" * len(DATA)
    assert store_bytes(store(ctx, "secondary"), PATH) == DATA
    verify_file.fn(original.copy_id)
    assert copy(ctx).state == FileState.UNKNOWN
    assert_invariants(ctx)


@pytest.mark.req("STO-03", "SVC-20")
def test_move_rechecks_destination_before_removing_the_source(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_id = upload(ctx)
    real_delete = transfer.delete.delete_object
    intervened = False

    def lose_destination(loc: StorageLocation, snap: CopySnapshot, **kwargs: Any) -> FileState:
        nonlocal intervened
        if snap.storage_location == "primary" and not intervened:
            intervened = True
            assert transfer.delete_source_copy.fn(file_id, "secondary").outcome == "deleted"
        return real_delete(loc, snap, **kwargs)

    monkeypatch.setattr(transfer.delete, "delete_object", lose_destination)
    with pytest.raises(tr.StaleOperation):
        transfer.transfer_file.fn(file_id, "primary", "secondary", action="move")
    assert store_bytes(store(ctx), PATH) == DATA
    assert copy(ctx).state == FileState.UNKNOWN
    assert copy(ctx).token is None
    assert copy(ctx, "secondary").state == FileState.DELETED
    assert repair_expired(copy(ctx).copy_id) == RepairResult.NOT_OWNED_OR_LIVE
    assert_invariants(ctx)


@pytest.mark.req("SVC-20")
def test_source_delete_does_not_take_over_another_copys_upload(ctx: AppContext) -> None:
    file_id = upload(ctx)
    upload(ctx, storage_location="secondary")
    with ctx.write_session() as session:
        reservation = uploads.reserve(
            session,
            project="demo",
            storage_location="secondary",
            path=PATH,
            hash=md5(OTHER),
            size=len(OTHER),
            lease=ctx.settings.op_lease,
            overwrite=True,
        )
    other_owner = copy(ctx, "secondary")
    assert transfer.delete_source_copy.fn(file_id, "primary").outcome == "deleted"
    assert copy(ctx, "secondary") == other_owner
    assert reservation.write_path is not None and reservation.token is not None
    put(store(ctx, "secondary"), reservation.write_path, OTHER)
    assert complete_upload(reservation.token).state == FileState.AVAILABLE
    assert_invariants(ctx)


@pytest.mark.req("SVC-19")
@pytest.mark.parametrize("lose_lease", [False, True])
def test_blocking_copy_renews_destination_and_stops_if_renewal_fails(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch, lose_lease: bool
) -> None:
    file_id = upload(ctx)
    original = copy(ctx)
    ctx.settings.op_lease = timedelta(milliseconds=30)
    renewed = threading.Event()
    real_renew = uploads.renew
    real_copy = transfer._copy_bytes
    real_reserve = transfer.transfers.reserve_destination
    reservations: list[CopySnapshot] = []

    def reserve(*args: Any, **kwargs: Any) -> CopySnapshot:
        snap = real_reserve(*args, **kwargs)
        reservations.append(snap)
        return snap

    def renew(*args: Any, **kwargs: Any) -> Any:
        if lose_lease:
            renewed.set()
            raise uploads.TokenNotCurrent("transfer lease lost")
        result = real_renew(*args, **kwargs)
        renewed.set()
        return result

    def block(*args: Any, **kwargs: Any) -> None:
        assert renewed.wait(5), "the destination was not renewed during a blocking transfer"
        if lose_lease:
            for worker in threading.enumerate():
                if worker.name.startswith("transfer-lease-"):
                    worker.join(timeout=5)
        else:
            before = reservations[0].deadline
            with ctx.write_session() as session:
                row = session.get_one(FileStorageLocation, reservations[0].copy_id)
                after = row.op_deadline
            assert before is not None and after is not None and after > before
        real_copy(*args, **kwargs)

    monkeypatch.setattr(transfer.transfers, "reserve_destination", reserve)
    monkeypatch.setattr(uploads, "renew", renew)
    monkeypatch.setattr(transfer, "_copy_bytes", block)
    if lose_lease:
        with pytest.raises(uploads.TokenNotCurrent, match="transfer lease lost"):
            transfer.transfer_file.fn(file_id, "primary", "secondary", action="move")
        assert copy(ctx) == original
    else:
        assert transfer.transfer_file.fn(file_id, "primary", "secondary").outcome == "copied"
    assert not any(t.name.startswith("transfer-lease-") for t in threading.enumerate())
    assert_invariants(ctx)

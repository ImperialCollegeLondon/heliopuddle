"""Transfer service: interrupted moves and destination publication."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from pydantic import ByteSize
from sqlalchemy import select

from heliopuddle.config.models import RuleOptions
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import FileStorageLocation
from heliopuddle.server.database.orm import StorageLocation as StorageLocationRow
from heliopuddle.server.models import transitions as tr
from heliopuddle.server.models.files import InactiveError, NotFoundError
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services import transfer
from heliopuddle.server.services import upload as upload_service
from heliopuddle.server.services.upload import (
    RepairResult,
    repair_expired,
)
from heliopuddle.server.services.verify import verify_file
from heliopuddle.storage import StorageLocation
from heliopuddle.storage.location import backup_path
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


@pytest.mark.req("STO-03", "SVC-20")
def test_source_delete_repair_rehashes_even_if_size_and_mtime_are_unchanged(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_id = upload(ctx)
    original_info = store(ctx).info(PATH)
    real_rm = StorageLocation.rm
    real_info = StorageLocation.info

    def fail_remove(loc: StorageLocation, path: str) -> None:
        if loc.url == store(ctx).url:
            raise OSError("source is temporarily read-only")
        real_rm(loc, path)

    monkeypatch.setattr(StorageLocation, "rm", fail_remove)
    with pytest.raises(RuntimeError, match="deleting"):
        transfer.transfer_file.fn(file_id, "primary", "secondary", action="move")
    monkeypatch.setattr(StorageLocation, "rm", real_rm)
    put(store(ctx), PATH, b"x" * len(DATA))

    def preserved_metadata(loc: StorageLocation, path: str) -> Any:
        if loc.url == store(ctx).url and path == PATH:
            return original_info
        return real_info(loc, path)

    monkeypatch.setattr(StorageLocation, "info", preserved_metadata)
    assert repair_expired(copy(ctx).copy_id) == RepairResult.REPAIRED
    assert store_bytes(store(ctx), PATH) == b"x" * len(DATA)
    assert copy(ctx).state == FileState.UNKNOWN
    assert_invariants(ctx)


@pytest.mark.req("STO-03", "SVC-19")
@pytest.mark.parametrize("destination_after", ["intact", "removed", "altered"])
def test_interrupted_move_delete_is_retried_only_while_the_destination_holds(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch, destination_after: str
) -> None:
    file_id = upload(ctx)
    real_rm = StorageLocation.rm

    def fail_remove(loc: StorageLocation, path: str) -> None:
        if loc.url == store(ctx).url:
            raise OSError("source is temporarily read-only")
        real_rm(loc, path)

    monkeypatch.setattr(StorageLocation, "rm", fail_remove)
    with pytest.raises(RuntimeError, match="deleting"):
        transfer.transfer_file.fn(file_id, "primary", "secondary", action="move")
    monkeypatch.setattr(StorageLocation, "rm", real_rm)
    source = copy(ctx)
    assert (source.state, source.token) == (FileState.DELETING, None)
    assert source.move_destination_id == copy(ctx, "secondary").copy_id
    if destination_after == "removed":
        store(ctx, "secondary").rm(PATH)
    elif destination_after == "altered":
        put(store(ctx, "secondary"), PATH, OTHER)

    assert repair_expired(source.copy_id) == RepairResult.REPAIRED
    repaired = copy(ctx)
    if destination_after == "intact":
        assert repaired.state == FileState.DELETED
        assert store_bytes(store(ctx), PATH) is None
    else:
        # The source may be the last valid copy: kept, and re-checked by the index.
        assert (repaired.state, repaired.token) == (FileState.PENDING, None)
        assert repaired.move_destination_id is None
        assert store_bytes(store(ctx), PATH) == DATA
        return  # the destination was changed outside hp; its row is stale until an index
    assert_invariants(ctx)


@pytest.mark.req("STO-03", "SVC-19")
def test_an_ordinary_delete_take_over_clears_a_move_destination_guard(ctx: AppContext) -> None:
    file_id = upload(ctx)
    upload(ctx, storage_location="secondary")
    source, destination = copy(ctx), copy(ctx, "secondary")
    with ctx.write_session() as session:
        owned = transfer.transfers.begin_source_delete(
            session,
            source,
            store(ctx).url,
            ctx.settings.op_lease,
            destination=destination,
        )
    assert owned.move_destination_id == destination.copy_id
    with ctx.write_session() as session:
        row = session.get_one(FileStorageLocation, owned.copy_id)
        tr.release(row, FileState.AVAILABLE)
    assert copy(ctx).move_destination_id is None
    assert file_id


@pytest.mark.req("STO-03", "SVC-19")
@pytest.mark.parametrize("existing", [False, True])
def test_corrupt_copied_bytes_never_publish_or_remove_the_source(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch, existing: bool
) -> None:
    file_id = upload(ctx)
    if existing:
        put(store(ctx, "secondary"), PATH, OTHER)
    original = copy(ctx)
    real_copy = transfer._copy_bytes

    def corrupt(
        source: StorageLocation,
        destination: StorageLocation,
        path: str,
        write_path: str,
        check: Any,
    ) -> None:
        real_copy(source, destination, path, write_path, check)
        put(destination, write_path, b"corrupt")

    monkeypatch.setattr(transfer, "_copy_bytes", corrupt)
    monkeypatch.setattr(StorageLocation, "native_md5", lambda *_args: md5(DATA))
    with pytest.raises(transfer.TransferVerificationError):
        transfer.transfer_file.fn(
            file_id, "primary", "secondary", action="move", options=RuleOptions(overwrite=True)
        )
    assert copy(ctx) == original
    assert store_bytes(store(ctx), PATH) == DATA
    if existing:
        assert store_bytes(store(ctx, "secondary"), PATH) == OTHER
    assert repair_expired(copy(ctx, "secondary").copy_id) == RepairResult.DROPPED
    assert store_bytes(store(ctx, "secondary"), PATH) == (OTHER if existing else None)
    assert_invariants(ctx)


@pytest.mark.req("STO-03", "SVC-19")
def test_bad_publication_restores_a_backup_and_never_deletes_the_source(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_id = upload(ctx)
    put(store(ctx, "secondary"), PATH, OTHER)
    source = copy(ctx)
    destination = store(ctx, "secondary")
    real_publish = transfer._move_into_place

    def corrupt(loc: StorageLocation, final: str, staged: str, backup: str) -> None:
        real_publish(loc, final, staged, backup)
        put(loc, final, b"bad publication")

    monkeypatch.setattr(transfer, "_move_into_place", corrupt)
    with pytest.raises(transfer.TransferVerificationError):
        transfer.transfer_file.fn(
            file_id, "primary", "secondary", action="move", options=RuleOptions(overwrite=True)
        )
    assert copy(ctx) == source
    assert store_bytes(store(ctx), PATH) == DATA
    if not destination.move_replaces:
        assert store_bytes(destination, PATH) == OTHER
    assert repair_expired(copy(ctx, "secondary").copy_id) == RepairResult.DROPPED
    if not destination.move_replaces:
        assert store_bytes(destination, PATH) == OTHER
    assert_invariants(ctx)


@pytest.mark.req("STO-04", "STO-14")
def test_quota_counts_unknown_copys_observed_size_not_its_logical_file_size(
    ctx: AppContext,
) -> None:
    used = upload(ctx, b"x" * 100, path="used.dat")
    put(store(ctx, "secondary"), "used.dat", b"x" * 40)
    assert transfer.transfer_file.fn(used, "primary", "secondary").outcome == "conflict"
    assert copy(ctx, "secondary", "used.dat").size == 40
    file_id = upload(ctx)
    ctx.projects["demo"].storage_location("secondary").quota = ByteSize(80)
    result = transfer.transfer_file.fn(
        file_id,
        "primary",
        "secondary",
        options=RuleOptions(check_for_space_overhead=ByteSize(10)),
    )
    assert result.outcome == "copied"
    assert_invariants(ctx)


@pytest.mark.req("SVC-19", "STO-03")
def test_crashed_publication_repairs_invalid_final_from_its_backup(
    ctx: AppContext, clock: Clock
) -> None:
    file_id = upload(ctx)
    original = copy(ctx)
    destination = store(ctx, "secondary")
    put(destination, PATH, OTHER)
    with ctx.write_session() as session:
        plan = transfer.transfers.transfer_snapshot(session, file_id, "primary", "secondary")
        owned = transfer.transfers.reserve_destination(
            session, plan, staged=True, lease=ctx.settings.op_lease
        )
    assert owned.token is not None
    put(destination, backup_path(PATH, owned.token), OTHER)
    put(destination, PATH, b"invalid final after worker crash")
    clock.advance(timedelta(hours=2))
    assert repair_expired(owned.copy_id) == RepairResult.DROPPED
    assert store_bytes(destination, PATH) == OTHER
    assert temp_files(destination) == []
    assert copy(ctx) == original
    assert_invariants(ctx)


@pytest.mark.req("SVC-19", "STO-03")
@pytest.mark.parametrize(
    "invalid",
    [
        "file",
        "source",
        "source-copy",
        "destination",
        "inactive",
        "same-name",
        "same-object",
        "action",
    ],
)
def test_invalid_or_aliased_endpoints_are_rejected_without_store_changes(
    ctx: AppContext, invalid: str
) -> None:
    file_id = upload(ctx)
    original = copy(ctx)
    source, destination, action = "primary", "secondary", "move"
    if invalid == "file":
        file_id += 100
    elif invalid == "source":
        source = "absent"
    elif invalid == "source-copy":
        source, destination = destination, source
    elif invalid == "destination":
        destination = "absent"
    elif invalid == "inactive":
        with ctx.write_session() as session:
            row = session.scalars(
                select(StorageLocationRow).where(StorageLocationRow.name == destination)
            ).one()
            row.active = False
    elif invalid == "same-name":
        destination = source
    elif invalid == "same-object":
        ctx.projects["demo"].storage_location(destination).url = store(ctx).url
        ctx.sync_config()
    else:
        action = "delete"
    with pytest.raises((NotFoundError, InactiveError, ValueError)):
        transfer.transfer_file.fn(file_id, source, destination, action=action)
    assert copy(ctx) == original
    assert store_bytes(store(ctx), PATH) == DATA
    assert_invariants(ctx)


@pytest.mark.req("SVC-19")
@pytest.mark.parametrize("repair_fails", [False, True])
def test_expired_destination_is_repaired_before_real_work_but_not_dry_run(
    ctx: AppContext,
    monkeypatch: pytest.MonkeyPatch,
    clock: Clock,
    repair_fails: bool,
) -> None:
    file_id = upload(ctx)
    with ctx.write_session() as session:
        plan = transfer.transfers.transfer_snapshot(session, file_id, "primary", "secondary")
        owned = transfer.transfers.reserve_destination(
            session, plan, staged=False, lease=ctx.settings.op_lease
        )
    put(store(ctx, "secondary"), PATH, b"abandoned partial transfer")
    clock.advance(timedelta(hours=2))
    with pytest.raises(tr.NeedsRepair):
        transfer.transfer_file.fn(
            file_id, "primary", "secondary", options=RuleOptions(dry_run=True)
        )
    assert copy(ctx, "secondary") == owned
    if repair_fails:
        monkeypatch.setattr(
            upload_service, "repair_expired", lambda *_args: RepairResult.INCOMPLETE
        )
        with pytest.raises(RuntimeError, match="repairing destination"):
            transfer.transfer_file.fn(file_id, "primary", "secondary")
        assert copy(ctx, "secondary") == owned
    else:
        assert transfer.transfer_file.fn(file_id, "primary", "secondary").outcome == "copied"
    assert_invariants(ctx)


@pytest.mark.req("SVC-19", "STO-04")
@pytest.mark.parametrize("previous", ["unknown", "deleted"])
def test_transfer_reuses_an_existing_unavailable_destination_copy(
    ctx: AppContext, previous: str
) -> None:
    file_id = upload(ctx)
    upload(ctx, storage_location="secondary")
    if previous == "deleted":
        transfer.delete_source_copy.fn(file_id, "secondary")
    else:
        put(store(ctx, "secondary"), PATH, OTHER)
        verify_file.fn(copy(ctx, "secondary").copy_id)
    before = copy(ctx, "secondary")
    source = copy(ctx)
    assert (
        transfer.transfer_file.fn(
            file_id, "primary", "secondary", options=RuleOptions(overwrite=True)
        ).outcome
        == "copied"
    )
    after = copy(ctx, "secondary")
    assert (after.copy_id, after.state) == (before.copy_id, FileState.AVAILABLE)
    assert after.generation > before.generation
    assert copy(ctx) == source
    assert_invariants(ctx)

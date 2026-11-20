"""Transfer service: copy, move, conflicts and dry runs."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select

from heliopuddle.config.models import RuleOptions
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import File, FileStorageLocation
from heliopuddle.server.models import transitions as tr
from heliopuddle.server.models import uploads
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services import transfer
from heliopuddle.server.services.upload import (
    RepairResult,
    repair_expired,
)
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
from tests.unit._transfer_helpers import (
    DATA,
    OTHER,
    PATH,
    copy,
    limits,
    upload,
)

_FIXTURES = (
    clock,
    limits,
)  # referenced so ruff sees the imports used


@pytest.mark.req("SVC-19", "STO-03")
@pytest.mark.parametrize("action", ["copy", "move"])
@pytest.mark.parametrize("native_copy", [True, False])
def test_transfer_reads_back_before_available_and_source_delete(
    ctx: AppContext,
    monkeypatch: pytest.MonkeyPatch,
    limits: list[str],
    action: str,
    native_copy: bool,
) -> None:
    file_id = upload(ctx)
    original = copy(ctx)
    monkeypatch.setattr(backends, "same_filesystem", lambda *_args: native_copy)
    events: list[str] = []
    real_observe = transfer.observe
    real_rm = StorageLocation.rm
    real_copy = store(ctx).fs.copy
    real_write = StorageLocation.write

    def observe(loc: StorageLocation, path: str) -> Any:
        obs = real_observe(loc, path)
        if loc.url == store(ctx, "secondary").url and path == PATH and obs is not None:
            events.append("verify")
        return obs

    def remove(loc: StorageLocation, path: str) -> None:
        if loc.url == store(ctx).url and path == PATH:
            assert "verify" in events
            assert copy(ctx, "secondary").state == FileState.AVAILABLE
            events.append("delete")
        real_rm(loc, path)

    def native(src: Any, dst: Any, **kwargs: Any) -> None:
        events.append("copy")
        assert copy(ctx).token is None
        real_copy(src, dst, **kwargs)

    def write(loc: StorageLocation, path: str, source: Any, **kwargs: Any) -> None:
        events.append("stream")
        assert copy(ctx).token is None
        real_write(loc, path, source, **kwargs)

    monkeypatch.setattr(transfer, "observe", observe)
    monkeypatch.setattr(StorageLocation, "rm", remove)
    monkeypatch.setattr(store(ctx).fs, "copy", native)
    monkeypatch.setattr(StorageLocation, "write", write)
    result = transfer.transfer_file.fn(file_id, "primary", "secondary", action=action)

    assert (result.file_id, result.path, result.outcome) == (
        file_id,
        PATH,
        "moved" if action == "move" else "copied",
    )
    assert ("copy" in events, "stream" in events) == (native_copy, not native_copy)
    assert events.index("copy" if native_copy else "stream") < events.index("verify")
    assert store_bytes(store(ctx, "secondary"), PATH) == DATA
    if action == "copy":
        assert copy(ctx) == original
    else:
        assert copy(ctx).state == FileState.DELETED
        assert store_bytes(store(ctx), PATH) is None
    assert limits == ["storage-location:demo:primary", "storage-location:demo:secondary"]
    assert_invariants(ctx)


@pytest.mark.req("STO-04")
@pytest.mark.parametrize(
    ("same", "overwrite", "newer_only", "newer", "outcome"),
    [
        (True, False, False, False, "copied"),
        (True, False, True, True, "copied"),
        (False, False, False, False, "conflict"),
        (False, False, True, True, "skipped"),
        (False, True, True, True, "skipped"),
        (False, True, True, False, "copied"),
        (False, True, False, True, "copied"),
    ],
)
def test_existing_destination_decisions(
    ctx: AppContext,
    monkeypatch: pytest.MonkeyPatch,
    same: bool,
    overwrite: bool,
    newer_only: bool,
    newer: bool,
    outcome: str,
) -> None:
    file_id = upload(ctx)
    put(store(ctx, "secondary"), PATH, DATA if same else OTHER)
    source = copy(ctx)
    assert source.mtime is not None
    real_observe = transfer.observe
    real_write = StorageLocation.write
    writes: list[str] = []
    monkeypatch.setattr(backends, "same_filesystem", lambda *_args: False)

    def observe(loc: StorageLocation, path: str) -> Any:
        obs = real_observe(loc, path)
        if obs is not None and loc.url == store(ctx, "secondary").url and path == PATH:
            return transfer.Observation(
                obs.size, source.mtime + timedelta(days=1 if newer else -1), obs.hash
            )
        return obs

    def write(loc: StorageLocation, path: str, stream: Any, **kwargs: Any) -> None:
        writes.append(path)
        assert path != PATH
        assert store_bytes(loc, PATH) == OTHER
        real_write(loc, path, stream, **kwargs)

    monkeypatch.setattr(transfer, "observe", observe)
    monkeypatch.setattr(StorageLocation, "write", write)
    result = transfer.transfer_file.fn(
        file_id,
        "primary",
        "secondary",
        options=RuleOptions(overwrite=overwrite, update_only_if_newer=newer_only),
    )
    assert result.outcome == outcome
    assert copy(ctx) == source
    assert store_bytes(store(ctx, "secondary"), PATH) == (DATA if outcome == "copied" else OTHER)
    assert bool(writes) == (not same and outcome == "copied")
    if outcome == "copied":
        assert copy(ctx, "secondary").state == FileState.AVAILABLE
    elif not (newer_only and newer):
        assert copy(ctx, "secondary").state == FileState.UNKNOWN
        assert result.detail
    assert_invariants(ctx)


@pytest.mark.req("SVC-19", "STO-04")
@pytest.mark.parametrize("action", ["copy", "move"])
def test_dry_run_changes_neither_db_nor_store(ctx: AppContext, action: str) -> None:
    file_id = upload(ctx)
    put(store(ctx, "secondary"), PATH, OTHER)
    before = copy(ctx)
    with ctx.sessionmaker() as session:
        rows_before = list(session.execute(select(FileStorageLocation.__table__)))
    result = transfer.transfer_file.fn(
        file_id,
        "primary",
        "secondary",
        action=action,
        options=RuleOptions(overwrite=True, dry_run=True),
    )
    assert result.outcome == f"would_{action}"
    assert copy(ctx) == before
    with ctx.sessionmaker() as session:
        assert list(session.execute(select(FileStorageLocation.__table__))) == rows_before
    assert store_bytes(store(ctx, "secondary"), PATH) == OTHER
    assert temp_files(store(ctx, "secondary")) == []
    assert_invariants(ctx)


@pytest.mark.req("SVC-19", "STO-03")
@pytest.mark.parametrize("phase", ["space", "write", "verify", "available"])
def test_pre_delete_failure_preserves_source(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    file_id = upload(ctx)
    before = copy(ctx)
    monkeypatch.setattr(backends, "same_filesystem", lambda *_args: False)

    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError(f"{phase} failed")

    if phase == "space":
        monkeypatch.setattr(backends, "free_space", fail)
    elif phase == "write":
        monkeypatch.setattr(StorageLocation, "write", fail)
    elif phase == "verify":
        real_observe = transfer.observe

        def verify(loc: StorageLocation, path: str) -> Any:
            if loc.url == store(ctx, "secondary").url and loc.exists(path):
                fail()
            return real_observe(loc, path)

        monkeypatch.setattr(transfer, "observe", verify)
    else:
        monkeypatch.setattr(transfer.transfers, "finish_transfer", fail)
    with pytest.raises(OSError, match=f"{phase} failed"):
        transfer.transfer_file.fn(file_id, "primary", "secondary", action="move")
    assert copy(ctx) == before
    assert store_bytes(store(ctx), PATH) == DATA
    assert_invariants(ctx)


@pytest.mark.req("SVC-19", "STO-03")
def test_source_overwrite_during_transfer_cannot_be_undone_or_deleted(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_id = upload(ctx)
    real_observe = transfer.observe
    changed = False

    def observe(loc: StorageLocation, path: str) -> Any:
        nonlocal changed
        obs = real_observe(loc, path)
        if loc.url == store(ctx, "secondary").url and obs is not None and not changed:
            changed = True
            assert copy(ctx).token is None
            upload(ctx, OTHER, overwrite=True)
        return obs

    monkeypatch.setattr(transfer, "observe", observe)
    with pytest.raises(tr.StaleOperation):
        transfer.transfer_file.fn(file_id, "primary", "secondary", action="move")
    assert copy(ctx).file_hash == md5(OTHER)
    assert store_bytes(store(ctx), PATH) == OTHER
    destination = copy(ctx, "secondary")
    assert repair_expired(destination.copy_id) == RepairResult.REPAIRED
    assert copy(ctx).file_hash == md5(OTHER)
    assert copy(ctx, "secondary").state == FileState.UNKNOWN
    assert_invariants(ctx)


@pytest.mark.req("SVC-19")
@pytest.mark.parametrize("existing", [False, True])
def test_expired_transfer_is_repaired_by_existing_upload_repair(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch, clock: Clock, existing: bool
) -> None:
    file_id = upload(ctx)
    if existing:
        put(store(ctx, "secondary"), PATH, OTHER)
    original = copy(ctx)
    real_finish = transfer.transfers.finish_transfer

    def crash(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("worker stopped")

    monkeypatch.setattr(transfer.transfers, "finish_transfer", crash)
    with pytest.raises(OSError, match="worker stopped"):
        transfer.transfer_file.fn(
            file_id, "primary", "secondary", action="move", options=RuleOptions(overwrite=True)
        )
    monkeypatch.setattr(transfer.transfers, "finish_transfer", real_finish)
    clock.advance(timedelta(hours=2))
    destination = copy(ctx, "secondary")
    with ctx.sessionmaker() as session:
        assert destination.copy_id in uploads.find_expired(
            session, destination.storage_location_id, clock()
        )
    assert repair_expired(destination.copy_id) == RepairResult.REPAIRED
    assert copy(ctx, "secondary").state == FileState.AVAILABLE
    assert copy(ctx) == original
    assert store_bytes(store(ctx), PATH) == DATA
    assert_invariants(ctx)


@pytest.mark.req("SVC-20", "STO-03")
def test_delete_is_scoped_and_soft_deletes_only_last_copy(ctx: AppContext) -> None:
    file_id = upload(ctx)
    upload(ctx, storage_location="secondary")
    secondary = copy(ctx, "secondary")
    result = transfer.delete_source_copy.fn(file_id, "primary", user="archiver")
    assert (result.file_id, result.path, result.outcome) == (file_id, PATH, "deleted")
    assert copy(ctx, "secondary") == secondary
    assert copy(ctx).state == FileState.DELETED
    assert store_bytes(store(ctx, "secondary"), PATH) == DATA
    assert_invariants(ctx)
    assert transfer.delete_source_copy.fn(file_id, "secondary").outcome == "deleted"
    with ctx.sessionmaker() as session:
        row = session.get_one(FileStorageLocation, secondary.copy_id)
        assert row.file.deleted_at is not None
    assert_invariants(ctx)


@pytest.mark.req("SVC-20", "STO-03")
def test_guarded_source_delete_rejects_replaced_generation(ctx: AppContext) -> None:
    file_id = upload(ctx)
    before = copy(ctx)
    upload(ctx, OTHER, overwrite=True)
    after = copy(ctx)
    with pytest.raises(tr.StaleOperation):
        transfer.delete_source_copy.fn(
            file_id,
            "primary",
            expected_generation=before.generation,
            expected_hash=before.file_hash,
        )
    assert copy(ctx) == after
    assert store_bytes(store(ctx), PATH) == OTHER
    assert_invariants(ctx)


@pytest.mark.req("SVC-20", "STO-03")
def test_failed_source_removal_is_a_failure_not_a_success_result(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_id = upload(ctx)
    real_rm = StorageLocation.rm

    def fail(loc: StorageLocation, path: str) -> None:
        if loc.url == store(ctx).url:
            raise PermissionError("cannot remove source")
        real_rm(loc, path)

    monkeypatch.setattr(StorageLocation, "rm", fail)
    with pytest.raises(RuntimeError, match="delet"):
        transfer.transfer_file.fn(file_id, "primary", "secondary", action="move")
    assert store_bytes(store(ctx), PATH) == DATA
    assert copy(ctx, "secondary").state == FileState.AVAILABLE
    assert copy(ctx).state == FileState.DELETING
    assert copy(ctx).token is None
    assert_invariants(ctx)


@pytest.mark.req("SVC-19")
@pytest.mark.parametrize("existing", [False, True])
def test_same_content_copy_does_not_change_logical_or_source_watermarks(
    ctx: AppContext, existing: bool
) -> None:
    file_id = upload(ctx)
    if existing:
        upload(ctx, storage_location="secondary")
    with ctx.sessionmaker() as session:
        file = session.get_one(File, file_id)
        logical = (file.updated_at, file.updated_by)
        source_at = session.get_one(FileStorageLocation, copy(ctx).copy_id).updated_at
    result = transfer.transfer_file.fn(file_id, "primary", "secondary", user="archiver")
    assert result.outcome == ("unchanged" if existing else "copied")
    with ctx.sessionmaker() as session:
        file = session.get_one(File, file_id)
        assert (file.updated_at, file.updated_by) == logical
        assert session.get_one(FileStorageLocation, copy(ctx).copy_id).updated_at == source_at
    assert_invariants(ctx)

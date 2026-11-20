"""Transfer service: races after reservation, limits and logging."""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select

from heliopuddle.config.models import RuleOptions
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import FileStorageLocation
from heliopuddle.server.models import transitions as tr
from heliopuddle.server.models.snapshots import CopySnapshot
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
from tests.lifecycle import store_bytes
from tests.unit._transfer_helpers import (  # noqa: F401
    DATA,
    OTHER,
    PATH,
    copy,
    limits,
    upload,
)

_FIXTURES = (clock,)  # referenced so ruff sees the imports used


@pytest.mark.req("SVC-19", "STO-04")
@pytest.mark.parametrize("arriving", ["same", "different", "overwrite", "newer"])
def test_object_appearing_after_reservation_is_never_directly_overwritten(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch, arriving: str
) -> None:
    file_id = upload(ctx)
    original = copy(ctx)
    real_reserve = transfer.transfers.reserve_destination
    real_write = StorageLocation.write
    writes: list[str] = []
    monkeypatch.setattr(backends, "same_filesystem", lambda *_args: False)

    def reserve(*args: Any, **kwargs: Any) -> CopySnapshot:
        owned = real_reserve(*args, **kwargs)
        assert not owned.staged
        put(store(ctx, "secondary"), PATH, DATA if arriving == "same" else OTHER)
        return owned

    def write(loc: StorageLocation, path: str, data: Any, **kwargs: Any) -> None:
        assert path != PATH
        assert store_bytes(loc, PATH) == OTHER
        writes.append(path)
        real_write(loc, path, data, **kwargs)

    monkeypatch.setattr(transfer.transfers, "reserve_destination", reserve)
    monkeypatch.setattr(StorageLocation, "write", write)
    result = transfer.transfer_file.fn(
        file_id,
        "primary",
        "secondary",
        options=RuleOptions(
            overwrite=arriving in {"overwrite", "newer"},
            update_only_if_newer=arriving == "newer",
        ),
    )
    expected = "copied" if arriving in {"same", "overwrite"} else "skipped"
    # A raced-in destination recorded as unknown is a change, so it reports a conflict.
    assert result.outcome == ("conflict" if arriving in {"different", "newer"} else expected)
    assert bool(writes) == (arriving == "overwrite")
    assert store_bytes(store(ctx, "secondary"), PATH) == (DATA if expected == "copied" else OTHER)
    assert copy(ctx) == original
    assert_invariants(ctx)


@pytest.mark.req("SVC-19")
def test_destination_created_after_observation_keeps_its_new_revision(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_id = upload(ctx)
    put(store(ctx, "secondary"), PATH, DATA)
    original = copy(ctx)
    real_observe = transfer.observe
    arrived = False

    def observe(loc: StorageLocation, path: str) -> Any:
        nonlocal arrived
        obs = real_observe(loc, path)
        if not arrived and loc.url == store(ctx, "secondary").url:
            arrived = True
            upload(ctx, storage_location="secondary")
        return obs

    monkeypatch.setattr(transfer, "observe", observe)
    with pytest.raises(tr.StaleOperation, match="created the destination"):
        transfer.transfer_file.fn(file_id, "primary", "secondary", action="move")
    assert copy(ctx) == original
    assert copy(ctx, "secondary").state == FileState.AVAILABLE
    assert_invariants(ctx)


@pytest.mark.req("SVC-19", "SVC-20")
def test_standalone_tasks_create_limits_before_acquiring_them(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_id = upload(ctx)
    created: list[str] = []
    acquired: list[list[str]] = []

    @contextmanager
    def acquire(names: str | list[str], **_kwargs: Any) -> Iterator[None]:
        requested = [names] if isinstance(names, str) else names
        assert all(name in created for name in requested), (
            "missing limits would silently be ignored"
        )
        acquired.append(requested)
        yield

    monkeypatch.setattr(transfer, "ensure_concurrency_limit", created.append)
    monkeypatch.setattr(transfer, "hp_concurrency", acquire)
    assert transfer.transfer_file.fn(file_id, "primary", "secondary").outcome == "copied"
    assert transfer.delete_source_copy.fn(file_id, "secondary").outcome == "deleted"
    assert acquired == [
        ["storage-location:demo:primary", "storage-location:demo:secondary"],
        ["storage-location:demo:secondary"],
    ]
    assert created == acquired[0] + acquired[1]
    assert_invariants(ctx)


@pytest.mark.req("SVC-19", "STO-03")
@pytest.mark.parametrize("existing_copy", [False, True])
def test_failed_native_copy_discards_its_partial_object_but_keeps_source(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch, existing_copy: bool
) -> None:
    file_id = upload(ctx)
    if existing_copy:
        upload(ctx, storage_location="secondary")
        transfer.delete_source_copy.fn(file_id, "secondary")
    original = copy(ctx)
    destination = store(ctx, "secondary")
    fs = store(ctx).fs

    def partial(_source: str, target: str) -> None:
        fs.pipe(target, DATA[:5])
        raise OSError("native copy failed mid-write")

    monkeypatch.setattr(fs, "copy", partial)
    with pytest.raises(OSError, match="native copy failed"):
        transfer.transfer_file.fn(file_id, "primary", "secondary", action="move")
    assert copy(ctx) == original
    assert store_bytes(store(ctx), PATH) == DATA
    assert not destination.exists(PATH)
    outcome = repair_expired(copy(ctx, "secondary").copy_id)
    assert outcome == (RepairResult.REPAIRED if existing_copy else RepairResult.DROPPED)
    assert_invariants(ctx)


@pytest.mark.req("SVC-19", "STO-03")
def test_failed_stream_after_takeover_cannot_clean_up_a_new_owners_object(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    file_id = upload(ctx)
    original = copy(ctx)
    real_read = StorageLocation.open_read
    monkeypatch.setattr(backends, "same_filesystem", lambda *_args: False)

    class Interrupted(io.BytesIO):
        first = True

        def read(self, size: int = -1) -> bytes:
            if self.first:
                self.first = False
                return DATA[:5]
            clock.advance(timedelta(hours=2))
            assert repair_expired(copy(ctx, "secondary").copy_id) == RepairResult.DROPPED
            upload(ctx, storage_location="secondary")
            raise OSError("source failed after destination takeover")

    def read(loc: StorageLocation, path: str) -> Any:
        if loc.url == store(ctx).url and path == PATH:
            return Interrupted()
        return real_read(loc, path)

    monkeypatch.setattr(StorageLocation, "open_read", read)
    with pytest.raises(OSError, match="source failed after destination takeover"):
        transfer.transfer_file.fn(file_id, "primary", "secondary")
    monkeypatch.setattr(StorageLocation, "open_read", real_read)
    assert copy(ctx) == original
    assert copy(ctx, "secondary").state == FileState.AVAILABLE
    assert store_bytes(store(ctx, "secondary"), PATH) == DATA
    assert_invariants(ctx)


@pytest.mark.req("SVC-19", "SVC-20")
def test_prefect_tasks_hold_real_limits_and_return_verified_results(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch, prefect_harness: None
) -> None:
    """Runs the transfer tasks through Prefect to check real global concurrency limits."""
    from heliopuddle.server.services import concurrency

    monkeypatch.setattr(transfer, "hp_concurrency", concurrency.hp_concurrency)
    monkeypatch.setattr(transfer, "ensure_concurrency_limit", concurrency.ensure_concurrency_limit)
    file_id = upload(ctx)
    moved = transfer.transfer_file.fn(file_id, "primary", "secondary", action="move")
    assert (moved.file_id, moved.path, moved.outcome) == (file_id, PATH, "moved")
    assert copy(ctx, "secondary").state == FileState.AVAILABLE
    assert copy(ctx).state == FileState.DELETED
    assert transfer.delete_source_copy.fn(file_id, "secondary").outcome == "deleted"
    assert_invariants(ctx)


@pytest.mark.req("STO-04", "SVC-06")
def test_an_identical_destination_never_fails_on_space(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_id = upload(ctx)
    assert transfer.transfer_file.fn(file_id, "primary", "secondary").outcome == "copied"
    monkeypatch.setattr(backends, "free_space", lambda *_args: 0)  # destination now full
    assert transfer.transfer_file.fn(file_id, "primary", "secondary").outcome == "unchanged"
    assert_invariants(ctx)


@pytest.mark.req("STO-04")
def test_a_dry_run_conflict_records_nothing_and_reports_skipped(ctx: AppContext) -> None:
    file_id = upload(ctx)
    put(store(ctx, "secondary"), PATH, OTHER)
    result = transfer.transfer_file.fn(
        file_id, "primary", "secondary", options=RuleOptions(dry_run=True)
    )
    assert result.outcome == "skipped"
    with ctx.sessionmaker() as session:
        names = {c.storage_location.name for c in session.scalars(select(FileStorageLocation))}
    assert names == {"primary"}


@pytest.mark.req("SVC-30")
@pytest.mark.parametrize("action", ["copy", "move"])
def test_transfer_logs_file_and_both_storage_locations(
    ctx: AppContext, caplog: pytest.LogCaptureFixture, action: str
) -> None:
    file_id = upload(ctx)

    with caplog.at_level(logging.INFO):
        transfer.transfer_file.fn(file_id, "primary", "secondary", action=action)  # type: ignore[arg-type]

    lines = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any(
        action in m and PATH in m and "primary -> secondary" in m and str(file_id) in m
        for m in lines
    ), lines

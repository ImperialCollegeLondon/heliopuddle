"""Index batches: repair budgets and resume safety."""

from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO

import pytest
from sqlalchemy import select, update

from heliopuddle.config.models import RuleOptions
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import File, FileStorageLocation, IndexProgress
from heliopuddle.server.database.orm import StorageLocation as StorageRow
from heliopuddle.server.models import index as index_db
from heliopuddle.server.models import transfers
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services import (
    index,
    index_run,
    transfer,
)
from heliopuddle.storage import StorageLocation
from tests.unit._index_batches_helpers import (
    limits,
    put,
    runner,
)

_FIXTURES = (runner,)  # referenced so ruff sees the imports used

pytestmark = pytest.mark.req("SVC-23")


@pytest.mark.req("SVC-19", "SVC-20", "STO-03")
def test_expired_delete_repairs_count_both_source_readbacks_and_resume(
    ctx: AppContext, store: Path, runner: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    for path in ("a.dat", "b.dat"):
        put(store, path)
    index.index_full.fn("demo", "primary")
    monkeypatch.setattr(transfer, "ensure_concurrency_limit", lambda *a, **k: None)
    monkeypatch.setattr(transfer, "hp_concurrency", lambda *a, **k: nullcontext())
    with ctx.sessionmaker() as session:
        ids = list(session.scalars(select(File.id).order_by(File.id)))
    for file_id in ids:
        result = transfer.transfer_file.fn(
            file_id, "primary", "secondary", options=RuleOptions(check_for_space=False)
        )
        assert result.outcome == "copied"
        with ctx.write_session() as session:
            source, url = transfers.source_snapshot(session, file_id, "primary")
            transfers.begin_source_delete(session, source, url, timedelta(seconds=-1))
    limits(ctx, max_files=None, max_bytes=1, max_duration=None, retrigger=False)
    reads: list[str] = []
    original_read = StorageLocation.open_read

    def read(location: StorageLocation, path: str) -> BinaryIO:
        reads.append(path)
        return original_read(location, path)

    monkeypatch.setattr(StorageLocation, "open_read", read)
    first = index.index_full.fn("demo", "primary")
    assert first.repaired == 1 and first.hashed_bytes == 20 and first.remaining
    assert reads == ["a.dat", "a.dat"]
    assert not (store / "a.dat").exists() and (store / "b.dat").exists()
    second = index.index_full.fn("demo", "primary")
    assert second.repaired == 1 and second.hashed_bytes == 20 and not second.remaining
    assert reads == ["a.dat", "a.dat", "b.dat", "b.dat"]
    assert not (store / "b.dat").exists()
    assert runner.successors == []
    with ctx.sessionmaker() as session:
        copies = session.scalars(
            select(FileStorageLocation).where(FileStorageLocation.storage_location_id == 2)
        ).all()
        assert len(copies) == 2 and all(c.state == FileState.AVAILABLE for c in copies)
        assert all(session.get_one(File, id).deleted_at is None for id in ids)


def test_storage_url_reset_cannot_be_undone_by_an_active_chain_checkpoint(
    ctx: AppContext, store: Path, runner: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    limits(ctx, max_files=1, retrigger=False)
    for i in range(3):
        put(store, f"{i}.dat")
    index.index_full.fn("demo", "primary")
    original = index_run.index_batch

    def reset_after_batch(*args, **kwargs):
        state = original(*args, **kwargs)
        with ctx.write_session() as session:
            index_db.reset_observations(session, 1)
            session.get_one(StorageRow, 1).url = f"file://{store.parent / 'replacement'}"
        return state

    monkeypatch.setattr(index_run, "index_batch", reset_after_batch)
    with pytest.raises(RuntimeError, match=r"changed|reset|stale"):
        index.index_full.fn("demo", "primary")
    with ctx.sessionmaker() as session:
        assert session.get(IndexProgress, 1) is None


@pytest.mark.req("SVC-23", "SVC-22")
def test_interrupted_full_chain_removes_nothing_and_a_rerun_finishes(
    ctx: AppContext, store: Path, runner: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("0.dat", "1.dat", "2.dat", "3.dat"):
        put(store, name)
    index.index_full.fn("demo", "primary")
    # 3.dat vanishes from the store, and the chain dies on 1.dat before reaching it.
    (store / "3.dat").unlink()
    limits(ctx, batch_size=1, max_files=1, retrigger=False)
    original = StorageLocation.md5

    def broken(self, path):
        if path == "1.dat":
            raise PermissionError(path)
        return original(self, path)

    monkeypatch.setattr(StorageLocation, "md5", broken)
    put(store, "1.dat", b"changed!!!")
    index.index_full.fn("demo", "primary")
    with pytest.raises(RuntimeError, match="failed"):
        index.index_full.fn("demo", "primary")
    with ctx.sessionmaker() as session:
        assert session.scalar(select(File.deleted_at).where(File.path == "3.dat")) is None
        assert session.scalar(select(File.deleted_at).where(File.path == "1.dat")) is None
    monkeypatch.setattr(StorageLocation, "md5", original)
    gone = None
    for _ in range(8):
        index.index_full.fn("demo", "primary")
        with ctx.sessionmaker() as session:
            gone = session.scalar(select(File.deleted_at).where(File.path == "3.dat"))
        if gone is not None:
            break
    assert gone is not None
    with ctx.sessionmaker() as session:
        copy = session.scalar(select(FileStorageLocation).join(File).where(File.path == "1.dat"))
        assert copy is not None and copy.file.deleted_at is None


@pytest.mark.req("SVC-22", "SVC-23")
def test_zero_pause_index_chain_finds_earlier_id_change_made_behind_its_cursor(
    ctx: AppContext, store: Path, runner: SimpleNamespace
) -> None:
    for i in range(4):
        put(store, f"{i}.dat")
    index.index_full.fn("demo", "primary")
    at = datetime.now(UTC) - timedelta(hours=1)
    with ctx.write_session() as session:
        session.execute(update(File).values(updated_at=at))
        session.execute(update(FileStorageLocation).values(updated_at=at))
        session.get_one(IndexProgress, 1).last_incremental_at = at
    ctx.projects["demo"].index.incremental_overlap = timedelta()
    ctx.projects["demo"].index.pause_between_runs = timedelta()
    limits(ctx, max_files=2, retrigger=True)
    first = index.index_incremental.fn("demo", "primary")
    assert first.processed == 2
    assert len(runner.successors) == 1
    # The earliest file changes behind the chain's cursor, after the chain started.
    put(store, "0.dat", b"changed behind the cursor")
    with ctx.write_session() as session:
        file = session.scalar(select(File).where(File.path == "0.dat"))
        assert file is not None
        file.updated_at = datetime.now(UTC)
    limits(ctx, max_files=10, retrigger=False)
    for _ in range(2):
        index.index_incremental.fn("demo", "primary")
    with ctx.sessionmaker() as session:
        copy = session.scalar(select(FileStorageLocation).join(File).where(File.path == "0.dat"))
        assert copy is not None
        assert copy.size == len(b"changed behind the cursor")

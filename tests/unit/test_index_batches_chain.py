"""Index batches: chaining, ties, limits and removals."""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select, update

from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import File, FileStorageLocation, IndexProgress
from heliopuddle.server.services import (
    index,
)
from heliopuddle.storage import StorageLocation
from heliopuddle.storage.location import ListEntry
from tests.unit._index_batches_helpers import (
    limits,
    put,
    runner,
)

_FIXTURES = (runner,)  # referenced so ruff sees the imports used

pytestmark = pytest.mark.req("SVC-23")


@pytest.mark.req("SVC-22")
@pytest.mark.slow
@pytest.mark.parametrize("mode", ["full", "incremental"])
def test_250_equal_timestamp_files_in_three_batches(
    ctx: AppContext, store: Path, runner: SimpleNamespace, mode: str
) -> None:
    limits(ctx)
    # Root files precede subdirectories, unlike a global path sort.
    paths = ["z-root.dat", *(f"a/{i:03}.dat" for i in range(249))]
    for path in paths:
        put(store, path)
    same = (datetime.now(UTC) - timedelta(hours=2)).timestamp()
    for path in paths:
        os.utime(store / path, (same, same))
    run = index.index_full.fn if mode == "full" else index.index_incremental.fn
    start = None
    for expected in (100, 100, 50):
        result = run("demo", "primary")
        assert result.added == expected
        start = start or result.started_at
        assert result.started_at == start
        with ctx.sessionmaker() as session:
            progress = session.get_one(IndexProgress, 1)
            if expected == 100:
                assert progress.last_full_at is None
    assert sum(map(len, runner.batches)) == 250
    assert len(runner.successors) == 2
    with ctx.sessionmaker() as session:
        assert len(session.scalars(select(File)).all()) == 250
        progress = session.get_one(IndexProgress, 1)
        assert progress.full_started_at is progress.incremental_started_at is None


@pytest.mark.req("SVC-22")
def test_incremental_db_ties_and_old_unknown_paths_are_not_skipped(
    ctx: AppContext, store: Path, runner: SimpleNamespace
) -> None:
    limits(ctx, max_files=1000)
    for i in range(250):
        put(store, f"{i:03}.dat")
    index.index_full.fn("demo", "primary")
    with ctx.write_session() as session:
        at = datetime.now(UTC) - timedelta(hours=1)
        session.execute(update(File).values(updated_at=at))
        session.execute(update(FileStorageLocation).values(updated_at=at))
        progress = session.get_one(IndexProgress, 1)
        progress.last_incremental_at = at
    ctx.projects["demo"].index.incremental_overlap = timedelta()
    limits(ctx, retrigger=False)
    runner.batches.clear()
    for count in (100, 100, 50):
        result = index.index_incremental.fn("demo", "primary")
        assert result.processed == count
    assert len({path for batch in runner.batches for path in batch}) == 250
    # This file's mtime is older than the saved watermark, but has never been indexed.
    put(store, "new-old.dat")
    assert index.index_incremental.fn("demo", "primary").added == 1


@pytest.mark.parametrize(
    ("batch", "expected"),
    [
        ({"max_files": 2, "max_bytes": None, "max_duration": None}, 2),
        ({"max_files": None, "max_bytes": 15, "max_duration": None}, 2),
        ({"max_files": 4, "max_bytes": 1, "max_duration": "30m"}, 1),
    ],
)
def test_index_limits_override_task_batch_size_and_resume_without_retrigger(
    ctx: AppContext, store: Path, runner: SimpleNamespace, batch: dict[str, Any], expected: int
) -> None:
    limits(ctx, batch_size=20, retrigger=False, **batch)
    for i in range(6):
        put(store, f"{i}.dat")
    first = index.index_full.fn("demo", "primary")
    assert first.added == expected
    assert first.hashed_bytes == expected * 10
    assert runner.successors == []
    second = index.index_full.fn("demo", "primary")
    assert second.added == expected
    with ctx.sessionmaker() as session:
        assert len(session.scalars(select(File)).all()) == expected * 2


def test_hash_bytes_are_actual_reads_not_native_hash_or_unchanged_sizes(
    ctx: AppContext, store: Path, runner: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    for path in ("a-native.dat", "b-read.dat", "c-read.dat"):
        put(store, path)
    original = StorageLocation.list_files

    def native_first(self, prefix=""):
        for entry in original(self, prefix):
            yield ListEntry(
                entry.path,
                entry.size,
                entry.mtime,
                hashlib.md5(b"0123456789").hexdigest() if entry.path.startswith("a-") else None,
            )

    monkeypatch.setattr(StorageLocation, "list_files", native_first)
    limits(ctx, max_files=None, max_bytes=1, max_duration=None, retrigger=False)
    result = index.index_full.fn("demo", "primary")
    assert result.added == 2
    assert result.hashed_bytes == 10
    assert index.index_full.fn("demo", "primary").added == 1
    result = index.index_full.fn("demo", "primary")
    assert result.processed == 3
    assert result.hashed_bytes == 0


def test_full_index_defers_removals_until_chain_end(
    ctx: AppContext, store: Path, runner: SimpleNamespace
) -> None:
    for i in range(5):
        put(store, f"{i}.dat")
    index.index_full.fn("demo", "primary")
    (store / "4.dat").unlink()
    limits(ctx, max_files=2, retrigger=False)
    first = index.index_full.fn("demo", "primary")
    with ctx.sessionmaker() as session:
        assert session.scalar(select(File.deleted_at).where(File.path == "4.dat")) is None
    assert first.removed == 0
    # A final removal phase can itself require a bounded continuation.
    for _ in range(4):
        index.index_full.fn("demo", "primary")
        with ctx.sessionmaker() as session:
            deleted = session.scalar(select(File.deleted_at).where(File.path == "4.dat"))
        if deleted is not None:
            break
    assert deleted is not None
    with ctx.sessionmaker() as session:
        progress = session.get_one(IndexProgress, 1)
        assert progress.last_full_at == first.started_at


def test_cursor_survives_deleted_resume_path_and_finds_late_earlier_path(
    ctx: AppContext, store: Path, runner: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    limits(ctx, max_files=1, retrigger=False)
    put(store, "z.dat")
    original = StorageLocation.md5

    def add_earlier(self, path):
        digest = original(self, path)
        put(store, "a/new.dat")
        put(store, "a-earlier.dat")
        return digest

    monkeypatch.setattr(StorageLocation, "md5", add_earlier)
    assert index.index_full.fn("demo", "primary").added == 1
    monkeypatch.setattr(StorageLocation, "md5", original)
    (store / "z.dat").unlink()
    for _ in range(4):
        index.index_full.fn("demo", "primary")
        with ctx.sessionmaker() as session:
            paths = set(session.scalars(select(File.path)).all())
        if paths == {"z.dat", "a/new.dat", "a-earlier.dat"}:
            break
    assert paths == {"z.dat", "a/new.dat", "a-earlier.dat"}


def test_failure_never_advances_past_failed_batch(
    ctx: AppContext, store: Path, runner: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    limits(ctx, batch_size=2, max_files=3, retrigger=False)
    for i in range(4):
        put(store, f"{i}.dat")
    original = StorageLocation.md5

    def broken(self, path):
        if path == "1.dat":
            raise PermissionError(path)
        return original(self, path)

    monkeypatch.setattr(StorageLocation, "md5", broken)
    with pytest.raises(RuntimeError, match="failed"):
        index.index_full.fn("demo", "primary")
    with ctx.sessionmaker() as session:
        progress = session.get(IndexProgress, 1)
        assert progress is None or progress.full_cursor_path in (None, "0.dat")
    monkeypatch.setattr(StorageLocation, "md5", original)
    index.index_full.fn("demo", "primary")
    with ctx.sessionmaker() as session:
        assert session.scalar(select(File).where(File.path == "1.dat")) is not None


def test_index_duration_limit_is_checked_inside_large_task_batch(
    ctx: AppContext, store: Path, runner: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from heliopuddle.server.services import batches

    limits(ctx, batch_size=20, max_files=None, max_bytes=None, max_duration="3s")
    for i in range(5):
        put(store, f"{i}.dat")
    clock = [0.0]
    monkeypatch.setattr(batches, "monotonic", lambda: clock[0])
    original = StorageLocation.md5

    def slow(self, path):
        value = original(self, path)
        clock[0] += 2
        return value

    monkeypatch.setattr(StorageLocation, "md5", slow)
    result = index.index_full.fn("demo", "primary")
    assert result.processed == result.added == 2
    assert result.hashed_bytes == 20
    assert len(runner.successors) == 1

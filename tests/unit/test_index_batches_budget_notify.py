"""Index batches: read budgets and change notifications."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from heliopuddle.config.models import VersionRule
from heliopuddle.server import models
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import File, FileStorageLocation
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services import (
    index,
    index_finish,
    rule_control,
)
from heliopuddle.storage import StorageLocation
from tests.unit._index_batches_helpers import (
    limits,
    put,
    runner,
)

_FIXTURES = (runner,)  # referenced so ruff sees the imports used

pytestmark = pytest.mark.req("SVC-23")


def test_hash_bytes_include_discarded_observation_reads(
    ctx: AppContext, store: Path, runner: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    limits(ctx, max_files=None, max_bytes=1, max_duration=None, retrigger=False)
    put(store, "a-changing.dat")
    put(store, "b.dat")
    original = StorageLocation.md5

    def changing(self, path):
        digest = original(self, path)
        if path == "a-changing.dat":
            put(store, path, b"changed during the read")
        return digest

    monkeypatch.setattr(StorageLocation, "md5", changing)
    result = index.index_full.fn("demo", "primary")
    assert result.processed == result.skipped == 1
    assert result.hashed_bytes == 10
    assert result.added == 0


def test_incremental_prefixes_share_the_streamed_directory_order(
    ctx: AppContext, store: Path, runner: SimpleNamespace
) -> None:
    limits(ctx, max_files=1, retrigger=False)
    ctx.projects["demo"].index.incremental_prefixes = ["a-child", "a/sub"]
    put(store, "a-child/one.dat")
    put(store, "a/sub/two.dat")
    assert index.index_incremental.fn("demo", "primary").added == 1
    assert index.index_incremental.fn("demo", "primary").added == 1
    assert [path for batch in runner.batches for path in batch] == [
        "a/sub/two.dat",
        "a-child/one.dat",
    ]


def test_failed_hash_reads_still_consume_the_run_budget(
    ctx: AppContext, store: Path, runner: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    limits(ctx, batch_size=10, max_files=None, max_bytes=1, max_duration=None, retrigger=False)
    put(store, "a.dat")
    put(store, "b.dat")
    original = StorageLocation.md5

    def broken(self, path):
        if path == "a.dat":
            with self.open_read(path) as stream:
                stream.read(10)
            raise OSError("hash failed after reading")
        return original(self, path)

    monkeypatch.setattr(StorageLocation, "md5", broken)
    with pytest.raises(RuntimeError, match="failed"):
        index.index_full.fn("demo", "primary")
    with ctx.sessionmaker() as session:
        assert session.scalars(select(File)).all() == []


def test_full_repair_hash_reads_are_bounded_too(
    ctx: AppContext, store: Path, runner: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    limits(ctx, max_files=None, max_bytes=1, max_duration=None, retrigger=False)
    copies: dict[int, str] = {}
    with ctx.write_session() as session:
        for path in ("a.dat", "b.dat"):
            put(store, path)
            result = models.register_file(
                session,
                project="demo",
                storage_location="primary",
                path=path,
                hash=hashlib.md5(b"0123456789").hexdigest(),
                size=10,
            )
            copy = session.get_one(FileStorageLocation, result.copy_id)
            copy.op_token = str(uuid4())
            copy.op_deadline = datetime.now(UTC) - timedelta(days=1)
            copy.mtime = datetime.now(UTC) - timedelta(hours=2)
            copies[result.copy_id] = path
    reads: list[str] = []

    def repair(id, now):
        StorageLocation(f"file://{store}").md5(copies[id])
        reads.append(copies[id])
        return "repaired"

    monkeypatch.setattr(index_finish, "_find_expired", lambda *a: list(copies))
    monkeypatch.setattr(index_finish, "_repair_expired", repair)
    result = index.index_full.fn("demo", "primary")
    assert result.hashed_bytes == 10 and result.repaired == 1 and result.remaining
    result = index.index_full.fn("demo", "primary")
    assert result.hashed_bytes == 10 and result.repaired == 1 and not result.remaining
    assert reads == ["a.dat", "b.dat"]


@pytest.mark.req("SVC-24")
def test_index_batch_notifies_once_after_commits_including_latest_flips(
    ctx: AppContext, store: Path, runner: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.projects["demo"].versioning = [
        VersionRule(paths=["**"], extraction_pattern=r"_v(?P<version>\d+)\.dat$")
    ]
    location = StorageLocation(f"file://{store}")
    put(store, "data_v1.dat")
    runner.batch(
        "demo", "primary", [index.IndexEntry.from_listing(e) for e in location.list_files()]
    )
    for path in ("data_v2.dat", "data_v3.dat"):
        put(store, path)
    entries = [
        index.IndexEntry.from_listing(e) for e in location.list_files() if e.path != "data_v1.dat"
    ]
    active_sessions: set[Session] = set()
    calls: list[tuple[AppContext, str, tuple[int, ...], int]] = []
    observed: list[dict[int, bool]] = []

    def started(session: Session, transaction: Any) -> None:
        if transaction.parent is None:
            active_sessions.add(session)

    def ended(session: Session, transaction: Any) -> None:
        if transaction.parent is None:
            active_sessions.remove(session)

    def notify(context: AppContext, project: str, file_ids: Iterable[int]) -> None:
        calls.append((context, project, tuple(file_ids), len(active_sessions)))
        with ctx.sessionmaker() as session:
            observed.append(dict(session.execute(select(File.id, File.is_latest)).all()))

    monkeypatch.setattr(rule_control, "notify_file_changes", notify)
    event.listen(ctx.sessionmaker, "after_transaction_create", started)
    event.listen(ctx.sessionmaker, "after_transaction_end", ended)
    try:
        result = runner.batch("demo", "primary", entries)
        assert result.added == 2
        assert calls == [(ctx, "demo", (1, 2, 3), 0)]
        assert observed == [{1: False, 2: False, 3: True}]
        assert result.changed_file_ids == [1, 2, 3]
        assert runner.batch("demo", "primary", entries).changed_file_ids == []
        assert len(calls) == 1
    finally:
        event.remove(ctx.sessionmaker, "after_transaction_create", started)
        event.remove(ctx.sessionmaker, "after_transaction_end", ended)


@pytest.mark.req("SVC-24")
def test_index_notification_failure_is_logged_without_failing_committed_work(
    ctx: AppContext,
    store: Path,
    runner: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    put(store, "one.dat")

    def unavailable(*args: Any) -> None:
        raise OSError("notification dispatch unavailable")

    monkeypatch.setattr(rule_control, "notify_file_changes", unavailable)
    entries = [
        index.IndexEntry.from_listing(e) for e in StorageLocation(f"file://{store}").list_files()
    ]
    with caplog.at_level(logging.ERROR):
        result = runner.batch("demo", "primary", entries)
    assert result.added == 1
    assert result.changed_file_ids == [1]
    assert "notification dispatch unavailable" in caplog.text
    with ctx.sessionmaker() as session:
        assert session.get_one(FileStorageLocation, 1).state == FileState.AVAILABLE


@pytest.mark.req("SVC-24")
def test_failed_index_batch_still_notifies_its_committed_changes(
    ctx: AppContext, store: Path, runner: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    for path in ("a-good.dat", "z-bad.dat"):
        put(store, path)
    original_hash = StorageLocation.md5
    calls: list[tuple[int, ...]] = []

    def hash_file(location: StorageLocation, path: str) -> str:
        if path == "z-bad.dat":
            raise OSError("object could not be read")
        return original_hash(location, path)

    monkeypatch.setattr(StorageLocation, "md5", hash_file)
    monkeypatch.setattr(
        rule_control, "notify_file_changes", lambda ctx, project, ids: calls.append(tuple(ids))
    )
    entries = [
        index.IndexEntry.from_listing(e) for e in StorageLocation(f"file://{store}").list_files()
    ]
    with pytest.raises(index.IndexBatchError, match=r"z-bad\.dat") as error:
        runner.batch("demo", "primary", entries)
    assert error.value.counts.changed_file_ids == [1]
    assert calls == [(1,)]
    with ctx.sessionmaker() as session:
        assert list(session.scalars(select(File.path))) == ["a-good.dat"]

"""Index flow: incremental runs, summary and DB races."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update

from heliopuddle.server import models
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import File, FileStorageLocation, utcnow
from heliopuddle.server.models import index as index_db
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services import index as index_mod
from heliopuddle.server.services import index_diff
from heliopuddle.server.services.index import (
    IndexEntry,
    IndexSummary,
    expand_prefixes,
    index_batch,
    index_full,
    index_incremental,
)
from heliopuddle.server.services.index_names import SYSTEM_USER
from heliopuddle.storage import StorageLocation
from tests.unit._index_helpers import (  # noqa: F401
    OLD,
    _md5,
    by_path,
    copy_state,
    put,
    register,
    set_index,
    stores,
)

_FIXTURES = (stores,)  # referenced so ruff sees the imports used

pytestmark = pytest.mark.usefixtures("inline_prefect")


# --- incremental --------------------------------------------------------------------


@pytest.mark.req("SVC-08")
def test_incremental_uses_watermark_with_overlap(ctx: AppContext, stores: dict[str, Path]) -> None:
    root = stores["primary"]
    put(root, "i/old.dat", b"old", age=timedelta(days=3))

    first = index_incremental.fn("demo", "primary")  # no watermark: everything
    assert first.added == 1
    with ctx.sessionmaker() as session:
        progress = index_db.get_index_progress(session, "demo", "primary")
        assert progress is not None
        assert first.started_at is not None
        settle = ctx.projects["demo"].index.settle_time
        assert progress.last_incremental_at == first.started_at - settle
        assert progress.last_full_at is None

    # Pretend the last run was an hour ago; overlap is 5 minutes.
    since = utcnow() - timedelta(hours=1)
    put(root, "i/before.dat", b"before", age=timedelta(hours=1, minutes=10))
    put(root, "i/overlap.dat", b"overlap", age=timedelta(hours=1, minutes=3))
    put(root, "i/after.dat", b"after", age=timedelta(minutes=30))
    put(root, "i/old.dat", b"old but changed", age=timedelta(days=2))

    summary = index_incremental.fn("demo", "primary", since=since)

    assert summary.listed == 4
    assert summary.added == 3
    with ctx.sessionmaker() as session:
        # M4 also adopts previously unknown paths with old mtimes.
        assert models.get_file_by_path(session, "demo", "i/before.dat") is not None
    assert by_path(ctx, "i/overlap.dat").is_latest
    # Its DB copy changed inside the selection window, independently of mtime.
    assert by_path(ctx, "i/old.dat").hash == _md5(b"old but changed")

    # The stored watermark is used when ``since`` is not given.
    with ctx.write_session() as session:
        row = models.get_storage_location(session, "demo", "primary")
        assert row is not None
        index_db.record_index_progress(session, row.id, "incremental", since)
    assert index_incremental.fn("demo", "primary").added == 0


@pytest.mark.req("SVC-08")
def test_incremental_never_detects_removals(ctx: AppContext, stores: dict[str, Path]) -> None:
    put(stores["primary"], "i/a.dat", b"a")
    index_full.fn("demo", "primary")
    (stores["primary"] / "i/a.dat").unlink()
    summary = index_incremental.fn("demo", "primary")
    assert (summary.removed, summary.missing) == (0, 0)
    assert copy_state(by_path(ctx, "i/a.dat"), "primary") == FileState.AVAILABLE


@pytest.mark.req("SVC-08")
def test_incremental_lists_only_expanded_prefixes(ctx: AppContext, stores: dict[str, Path]) -> None:
    root = stores["primary"]
    now = datetime.now(UTC)
    set_index(
        ctx,
        incremental_prefixes=["science/{date:%Y/%m/%d}", "missing/{date:%Y}"],
        incremental_lookback=timedelta(days=1),
    )
    for days in (0, 1, 5):
        put(root, f"science/{(now - timedelta(days=days)):%Y/%m/%d}/f.dat", b"%d" % days)
    put(root, "other/x.dat", b"x")

    summary = index_incremental.fn("demo", "primary")

    assert (summary.listed, summary.added) == (2, 2)
    with ctx.sessionmaker() as session:
        assert models.get_file_by_path(session, "demo", "other/x.dat") is None


@pytest.mark.req("SVC-08")
def test_expand_prefixes() -> None:
    now = datetime(2026, 10, 28, 1, 0, tzinfo=UTC)
    assert expand_prefixes([], now, timedelta(days=2)) == [""]
    assert expand_prefixes(["sci/{date:%Y/%m/%d}"], now, timedelta(days=2)) == [
        "sci/2026/10/26",
        "sci/2026/10/27",
        "sci/2026/10/28",
    ]
    # Overlapping prefixes are deduplicated; nested ones are dropped.
    assert expand_prefixes(
        ["sci/{date:%Y}/", "sci/{date:%Y}/{date:%m}", "/raw"], now, timedelta(days=400)
    ) == ["raw", "sci/2025", "sci/2026"]
    assert expand_prefixes(["a", "{date:%Y}", ""], now, timedelta(0)) == [""]


# --- summary artifact ------------------------------------------------------------------


@pytest.mark.req("SVC-03")
def test_summary_is_published_as_markdown_artifact(
    ctx: AppContext, stores: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    published: list[dict[str, Any]] = []
    monkeypatch.setattr(index_mod, "create_markdown_artifact", lambda **kw: published.append(kw))
    put(stores["primary"], "a.dat", b"a")
    summary = index_full.fn("demo", "primary")
    assert isinstance(summary, IndexSummary)
    (artifact,) = published
    assert artifact["key"] == "index-full-demo-primary"
    assert "| added | 1 |" in artifact["markdown"]
    assert index_mod._artifact_key("incremental", "My_Proj", "s3.Bucket") == (  # pyright: ignore[reportPrivateUsage]
        "index-incremental-my-proj-s3-bucket"
    )


@pytest.mark.req("SVC-03")
def test_unknown_file_counts_once(ctx: AppContext, stores: dict[str, Path]) -> None:
    put(stores["secondary"], "w/a.dat", b"a")
    index_full.fn("demo", "secondary")
    with ctx.sessionmaker() as session:
        rows = session.scalars(select(File)).all()
        assert len(rows) == 1 and rows[0].created_by == SYSTEM_USER


# --- races: the DB changed between the read and the write ------------------------------


@pytest.mark.req("SVC-03")
def test_new_path_registered_meanwhile_is_skipped(
    ctx: AppContext, stores: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    put(stores["primary"], "z/a.dat", b"a")
    real = index_diff.observe  # pyright: ignore[reportPrivateUsage]

    def client_registers_first(
        store: StorageLocation, entry: IndexEntry, *, read: bool = False
    ) -> index_diff.Observation | None:
        register(ctx, "primary", entry.path, b"a")
        return real(store, entry, read=read)

    monkeypatch.setattr(index_diff, "observe", client_registers_first)
    counts = index_batch.fn("demo", "primary", [IndexEntry(path="z/a.dat", size=1)])
    assert (counts.skipped, counts.added) == (1, 0)
    assert by_path(ctx, "z/a.dat").created_by is None


@pytest.mark.req("SVC-03")
def test_known_file_changed_meanwhile_is_skipped(
    ctx: AppContext, stores: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    put(stores["primary"], "z/b.dat", b"b")
    index_full.fn("demo", "primary")
    put(stores["primary"], "z/b.dat", b"b2", age=timedelta(hours=1))
    real = index_diff.observe  # pyright: ignore[reportPrivateUsage]
    with ctx.sessionmaker() as session:
        copy_id = session.scalars(
            select(FileStorageLocation.id).where(
                FileStorageLocation.file_id == by_path(ctx, "z/b.dat").id
            )
        ).one()

    def changed_meanwhile(
        store: StorageLocation, entry: IndexEntry, *, read: bool = False
    ) -> index_diff.Observation | None:
        with ctx.write_session() as session:
            models.set_copy_state(session, copy_id, FileState.UNKNOWN)
        return real(store, entry, read=read)

    monkeypatch.setattr(index_diff, "observe", changed_meanwhile)
    summary = index_full.fn("demo", "primary")
    assert (summary.skipped, summary.changed) == (1, 0)
    assert by_path(ctx, "z/b.dat").hash == _md5(b"b")


@pytest.mark.req("SVC-03")
def test_known_file_vanishing_before_hashing_is_skipped(
    ctx: AppContext, stores: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    put(stores["primary"], "z/c.dat", b"c")
    put(stores["secondary"], "z/c.dat", b"c")
    index_full.fn("demo", "secondary")  # a live file with no primary copy
    put(stores["primary"], "z/d.dat", b"d")
    index_full.fn("demo", "primary")
    put(stores["primary"], "z/d.dat", b"d2", age=timedelta(hours=1))
    with ctx.write_session() as session:
        session.execute(
            update(FileStorageLocation)
            .where(FileStorageLocation.file_id == by_path(ctx, "z/c.dat").id)
            .values(state=FileState.DELETED)
        )
    monkeypatch.setattr(index_diff, "observe", lambda store, entry, read=False: None)
    summary = index_full.fn("demo", "primary")
    assert summary.skipped == 2
    with ctx.sessionmaker() as session:
        assert index_db.get_index_targets(session, 1, 1, []) == {}


@pytest.mark.req("SVC-03")
def test_failed_removal_fails_the_flow(
    ctx: AppContext, stores: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    put(stores["primary"], "z/e.dat", b"e")
    index_full.fn("demo", "primary")
    (stores["primary"] / "z/e.dat").unlink()

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise OSError("db down")

    monkeypatch.setattr(index_db, "index_remove_copy", boom)
    with pytest.raises(RuntimeError, match="removals"):
        index_full.fn("demo", "primary")


@pytest.mark.req("SVC-30")
def test_index_logs_batch_and_flow_summaries_not_every_unchanged_file(
    ctx: AppContext, stores: dict[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    put(stores["primary"], "lg/a.dat", b"a")
    put(stores["primary"], "lg/b.dat", b"b")

    with caplog.at_level(logging.INFO):
        index_full.fn("demo", "primary")
    first = [r.getMessage() for r in caplog.records]
    assert any("batch of" in m and "1 added" in m for m in first), first
    assert any(
        m.startswith("index full of demo/primary finished") and "2 added" in m for m in first
    )

    caplog.clear()
    with caplog.at_level(logging.INFO):
        index_full.fn("demo", "primary")
    second = [r.getMessage() for r in caplog.records]
    assert not any("batch of" in m for m in second), second
    assert not any("lg/a.dat" in m for m in second), second
    assert any("0 added" in m for m in second)

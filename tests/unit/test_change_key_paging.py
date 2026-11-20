"""Change-key pages read the timestamp indexes and never skip or repeat (SVC-22, SVC-17)."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import event, select

from heliopuddle.config.models import RuleMatch
from heliopuddle.server import models
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.engine import refresh_statistics
from heliopuddle.server.database.orm import File, FileStorageLocation, Project, Series
from heliopuddle.server.models.index import incremental_candidates
from heliopuddle.server.models.rules import select_candidates
from heliopuddle.server.schemas import FileState
from heliopuddle.server.schemas.rules import RuleCursor

T0 = datetime(2026, 1, 1, tzinfo=UTC)
FILES = 400


def _populate(ctx: AppContext, seed: int) -> tuple[int, int, dict[int, tuple[datetime, int]]]:
    """Files whose file and copy update times interleave, with many exact ties."""
    rng = random.Random(seed)
    with ctx.write_session() as session:
        project_id = session.scalars(select(Project.id).where(Project.name == "demo")).one()
        location = models.get_storage_location(session, "demo", "primary")
        assert location is not None
        series = Series(project_id=project_id, series_key="p:k")
        session.add(series)
        session.flush()
        # Few distinct instants, so both timestamps tie across many files.
        file_times = [T0 + timedelta(seconds=rng.randrange(40)) for _ in range(FILES)]
        copy_times = [T0 + timedelta(seconds=rng.randrange(40)) for _ in range(FILES)]
        files = [
            File(
                project_id=project_id,
                path=f"d/{i:04d}.dat",
                name=f"{i:04d}.dat",
                series_id=series.id,
                size=1,
                hash="h",
                is_latest=False,
                updated_at=file_times[i],
            )
            for i in range(FILES)
        ]
        session.add_all(files)
        session.flush()
        session.add_all(
            FileStorageLocation(
                file_id=f.id,
                storage_location_id=location.id,
                state=FileState.AVAILABLE,
                size=1,
                updated_at=copy_times[i],
            )
            for i, f in enumerate(files)
        )
        session.flush()
        expected: dict[int, tuple[datetime, int]] = {
            f.id: (max(file_times[i], copy_times[i]), f.id) for i, f in enumerate(files)
        }
        return project_id, location.id, expected


def _naive(
    expected: dict[int, tuple[datetime, int]], since: datetime, through: datetime
) -> list[int]:
    keys = sorted(key for key in expected.values() if since <= key[0] <= through)
    return [file_id for _, file_id in keys]


@pytest.mark.req("SVC-22", "SVC-17")
@pytest.mark.parametrize("seed", [1, 2, 3])
@pytest.mark.parametrize("page", [1, 7, 100])
def test_incremental_index_pages_follow_the_change_key_exactly(
    ctx: AppContext, seed: int, page: int
) -> None:
    _, location_id, expected = _populate(ctx, seed)
    since, through = T0 + timedelta(seconds=10), T0 + timedelta(seconds=30)
    seen: list[int] = []
    after: RuleCursor | None = None
    with ctx.sessionmaker() as session:
        while True:
            rows = incremental_candidates(
                session, location_id, since=since, through=through, after=after, limit=page
            )
            if not rows:
                break
            assert len(rows) <= page
            seen += [row.file_id for row in rows]
            after = rows[-1].cursor
    assert seen == _naive(expected, since, through)


@pytest.mark.req("SVC-22", "SVC-04")
@pytest.mark.parametrize("seed", [4, 5])
@pytest.mark.parametrize("limit", [1, 13])
def test_rule_pages_follow_the_change_key_exactly(ctx: AppContext, seed: int, limit: int) -> None:
    project_id, location_id, expected = _populate(ctx, seed)
    changed_after = RuleCursor(updated_at=T0 + timedelta(seconds=5), id=FILES // 2)
    through = T0 + timedelta(seconds=35)
    match = RuleMatch(paths=["d/**"])
    seen: list[int] = []
    after: RuleCursor | None = None
    with ctx.sessionmaker() as session:
        while True:
            rows = select_candidates(
                session,
                project_id,
                location_id,
                match,
                now=T0 + timedelta(days=1),
                after=after,
                changed_after=changed_after,
                through=through,
                limit=limit,
            )
            if not rows:
                break
            seen += [row.file_id for row in rows]
            after = rows[-1].cursor
    want = sorted(
        key
        for key in expected.values()
        if (key[0], key[1]) > (changed_after.updated_at, changed_after.id) and key[0] <= through
    )
    assert seen == [file_id for _, file_id in want]


@pytest.mark.req("SVC-17")
@pytest.mark.parametrize("kind", ["index", "rule"])
def test_change_key_pages_seek_the_timestamp_indexes(ctx: AppContext, kind: str) -> None:
    """The emitted page queries read the ``(updated_at, id)`` indexes, not a full scan and sort."""
    project_id, location_id, _ = _populate(ctx, 6)
    cursor = RuleCursor(updated_at=T0 + timedelta(seconds=20), id=0)
    statements: list[tuple[str, Any]] = []

    def capture(_conn: Any, _cursor: Any, statement: str, parameters: Any, *_: Any) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append((statement, parameters))

    engine = ctx.sessionmaker.kw["bind"]
    event.listen(engine, "before_cursor_execute", capture)
    try:
        with ctx.sessionmaker() as session:
            if kind == "index":
                incremental_candidates(
                    session, location_id, since=None, through=T0 + timedelta(days=1), after=cursor
                )
            else:
                select_candidates(
                    session,
                    project_id,
                    location_id,
                    RuleMatch(paths=["d/**"]),
                    now=T0 + timedelta(days=1),
                    changed_after=cursor,
                )
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    refresh_statistics(engine)  # as startup and every finished full index do
    plans = []
    with engine.connect() as conn:
        for statement, parameters in statements:
            rows = conn.exec_driver_sql(f"EXPLAIN QUERY PLAN {statement}", parameters).all()
            plans.append(" | ".join(str(row[-1]) for row in rows))
    pages = [plan for plan in plans if "updated_at_id" in plan]
    assert any("ix_file_updated_at_id" in plan for plan in pages), plans
    assert any("ix_file_storage_location_updated_at_id" in plan for plan in pages), plans
    assert not any("USE TEMP B-TREE FOR ORDER BY" in plan for plan in pages), plans


@pytest.mark.req("SVC-17")
def test_refresh_statistics_is_bounded_and_never_raises(ctx: AppContext) -> None:
    _populate(ctx, 7)
    engine = ctx.sessionmaker.kw["bind"]
    refresh_statistics(engine)
    with engine.connect() as conn:
        tables = {row[0] for row in conn.exec_driver_sql("SELECT tbl FROM sqlite_stat1")}
    assert {"file", "file_storage_location"} <= tables

    class Broken:
        class dialect:
            name = "sqlite"

        def connect(self) -> Any:
            raise OSError("database is locked")

    refresh_statistics(Broken())  # type: ignore[arg-type]

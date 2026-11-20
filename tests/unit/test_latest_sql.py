"""``recompute_latest`` selects the top eligible version in SQL (SVC-17, API-12)."""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import event, func, select, update

from heliopuddle.server import models
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import File, FileStorageLocation, Project, Series, utcnow
from heliopuddle.server.schemas import FileState

S = FileState
VERSIONS = 2000


def _series(ctx: AppContext, key: str, version_type: str | None = "number") -> int:
    with ctx.write_session() as session:
        project_id = session.scalars(select(Project.id).where(Project.name == "demo")).one()
        series = Series(project_id=project_id, series_key=key, version_type=version_type)
        session.add(series)
        session.flush()
        return series.id


def _bulk(ctx: AppContext, series_id: int, rows: list[dict[str, Any]]) -> list[int]:
    """Insert files (one ``primary`` copy each); a row gives the file and copy fields."""
    with ctx.write_session() as session:
        project_id = session.scalars(select(Project.id).where(Project.name == "demo")).one()
        location_id = models.get_storage_location(session, "demo", "primary").id  # type: ignore[union-attr]
        base = utcnow()
        files = []
        for i, row in enumerate(rows):
            f = File(
                project_id=project_id,
                path=row.get("path", f"s/{series_id}/{i}"),
                name=str(i),
                series_id=series_id,
                version_number=row.get("version"),
                version_text=row.get("text"),
                size=1,
                hash="h",
                is_latest=False,
                created_at=row.get("created_at", base + timedelta(microseconds=i)),
                deleted_at=row.get("deleted_at"),
            )
            files.append(f)
        session.add_all(files)
        session.flush()
        session.add_all(
            FileStorageLocation(
                file_id=f.id,
                storage_location_id=location_id,
                state=row.get("state", S.AVAILABLE),
                op_token=row.get("token"),
            )
            for f, row in zip(files, rows, strict=True)
        )
        return [f.id for f in files]


def _recompute(ctx: AppContext, series_id: int) -> int | None:
    with ctx.write_session() as session:
        return models.recompute_latest(session, models.lock_series_by_id(session, series_id))


def _flagged(ctx: AppContext, series_id: int) -> list[int]:
    with ctx.sessionmaker() as session:
        return list(
            session.scalars(
                select(File.id).where(File.series_id == series_id, File.is_latest.is_(True))
            )
        )


@pytest.mark.req("SVC-17")
def test_two_thousand_versions_bounded_time_and_statements(ctx: AppContext) -> None:
    series_id = _series(ctx, "v:big_v{v}.dat")
    # The highest version is not eligible; the next one is.
    rows: list[dict[str, Any]] = [{"version": v} for v in range(VERSIONS)]
    rows[-1]["state"] = S.UNKNOWN
    ids = _bulk(ctx, series_id, rows)
    assert _recompute(ctx, series_id) == ids[-2]

    statements: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        statements.append(statement)

    event.listen(ctx.engine, "before_cursor_execute", record)
    try:
        start = time.perf_counter()
        with ctx.write_session() as session:
            series = models.lock_series_by_id(session, series_id)
            statements.clear()
            latest = models.recompute_latest(session, series)
        elapsed = time.perf_counter() - start
    finally:
        event.remove(ctx.engine, "before_cursor_execute", record)

    assert latest == ids[-2]
    assert elapsed < 0.5
    # One SELECT for the top file, two bulk UPDATEs, one series UPDATE at most:
    # nothing per member.
    assert len(statements) <= 4, statements
    assert sum(s.lstrip().upper().startswith("SELECT") for s in statements) == 1
    assert _flagged(ctx, series_id) == [ids[-2]]
    with ctx.sessionmaker() as session:
        assert session.scalar(select(func.count()).where(File.is_latest.is_(True))) == 1


@pytest.mark.req("SVC-17")
@pytest.mark.req("API-12")
@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        # A token-holding pending upload is not eligible; a token-less one is.
        ([{"version": 1}, {"version": 2, "state": S.PENDING, "token": "t"}], 0),
        ([{"version": 1}, {"version": 2, "state": S.PENDING}], 1),
        # An overwrite in progress keeps serving the old content.
        ([{"version": 1}, {"version": 2, "state": S.OVERWRITING, "token": "t"}], 1),
        ([{"version": 1}, {"version": 2, "state": S.DELETING, "token": "t"}], 0),
        ([{"version": 1, "state": S.MISSING}, {"version": 2, "state": S.UNKNOWN}], None),
        # Deleted files are never latest.
        ([{"version": 1}, {"version": 2, "deleted_at": utcnow()}], 0),
        # Equal versions: the newest created_at, then the highest id.
        ([{"version": 3}, {"version": 3}], 1),
        ([{"version": 3, "created_at": utcnow() + timedelta(days=1)}, {"version": 3}], 0),
    ],
)
def test_eligibility_and_ordering(
    ctx: AppContext, rows: list[dict[str, Any]], expected: int | None
) -> None:
    series_id = _series(ctx, "v:e_v{v}.dat")
    ids = _bulk(ctx, series_id, rows)
    want = None if expected is None else ids[expected]
    assert _recompute(ctx, series_id) == want
    assert _flagged(ctx, series_id) == ([] if want is None else [want])
    with ctx.sessionmaker() as session:
        assert session.get_one(Series, series_id).latest_file_id == want


@pytest.mark.req("SVC-17")
@pytest.mark.req("API-02")
@pytest.mark.parametrize(
    ("version_type", "rows", "expected"),
    [
        # Text compares byte-wise: zero-padded numbers order as numbers.
        ("text", [{"text": "001.0002"}, {"text": "001.0010"}], 1),
        ("text", [{"text": "002.0000"}, {"text": "001.9999"}], 0),
        ("text", [{"text": "B"}, {"text": "a"}], 1),  # bytes, not case-folded
        ("text", [{"text": "a.b"}, {"text": "a-c"}], 0),  # punctuation counts
        # Unversioned rows rank lowest, even when newer.
        ("text", [{"text": "0"}, {}], 0),
        ("number", [{"version": 0}, {}], 0),
        ("number", [{"version": 9}, {"version": 10}], 1),
        # Equal versions: the newest created_at, then the highest id.
        ("text", [{"text": "v"}, {"text": "v"}], 1),
        ("text", [{"text": "v", "created_at": utcnow() + timedelta(days=1)}, {"text": "v"}], 0),
        # A series without a kind orders by created_at, then id.
        (None, [{}, {}], 1),
    ],
)
def test_ordering_by_the_series_kind(
    ctx: AppContext, version_type: str | None, rows: list[dict[str, Any]], expected: int
) -> None:
    series_id = _series(ctx, "v:m_v{v}.dat", version_type)
    ids = _bulk(ctx, series_id, rows)
    assert _recompute(ctx, series_id) == ids[expected]


@pytest.mark.req("SVC-17")
def test_recompute_moves_the_flag_and_keeps_loaded_objects_in_step(ctx: AppContext) -> None:
    series_id = _series(ctx, "v:k_v{v}.dat")
    ids = _bulk(ctx, series_id, [{"version": 1}, {"version": 2}])
    _recompute(ctx, series_id)
    with ctx.write_session() as session:
        files = {i: session.get_one(File, i) for i in ids}
        copy = session.scalars(
            select(FileStorageLocation).where(FileStorageLocation.file_id == ids[1])
        ).one()
        copy.state = S.UNKNOWN
        assert (
            models.recompute_latest(session, models.lock_series_by_id(session, series_id)) == ids[0]
        )
        assert files[ids[0]].is_latest is True
        assert files[ids[1]].is_latest is False
    assert _flagged(ctx, series_id) == [ids[0]]


@pytest.mark.req("SVC-05")
def test_latest_flips_bump_both_timestamps_but_unchanged_flags_do_not(ctx: AppContext) -> None:
    series_id = _series(ctx, "v:changes_v{v}.dat")
    ids = _bulk(ctx, series_id, [{"version": 1}, {"version": 2}, {"version": 3}])
    _recompute(ctx, series_id)
    old = utcnow() - timedelta(days=1)
    with ctx.write_session() as session:
        session.execute(update(File).where(File.series_id == series_id).values(updated_at=old))
    with ctx.write_session() as session:
        files = {i: session.get_one(File, i) for i in ids}
        copy = session.scalars(
            select(FileStorageLocation).where(FileStorageLocation.file_id == ids[2])
        ).one()
        copy.state = S.UNKNOWN
        models.recompute_latest(session, models.lock_series_by_id(session, series_id))
        assert files[ids[0]].updated_at == old
        assert files[ids[1]].updated_at > old
        assert files[ids[2]].updated_at == files[ids[1]].updated_at
        timestamps = {i: f.updated_at for i, f in files.items()}
        models.recompute_latest(session, models.lock_series_by_id(session, series_id))
        assert {i: f.updated_at for i, f in files.items()} == timestamps
    with ctx.sessionmaker() as session:
        assert {i: session.get_one(File, i).updated_at for i in ids} == timestamps

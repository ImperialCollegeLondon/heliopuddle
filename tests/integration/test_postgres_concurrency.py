"""Concurrent writes in one series on Postgres (DB-02).

Threads, each with its own session from the pool, add and remove versions of
one series. The series row is locked with ``SELECT ... FOR UPDATE``; the
result must be exactly one live ``is_latest`` file, ``series.latest_file_id``
pointing at it, unique live paths and no deadlocks.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from sqlalchemy import func, select

from heliopuddle.server import models
from heliopuddle.server.context import AppContext, set_context
from heliopuddle.server.database.migrate import upgrade_to_head
from heliopuddle.server.database.orm import File, Series
from heliopuddle.server.services.delete import delete_file
from heliopuddle.settings import Settings

from .conftest import PROJECT, make_project_dirs

THREADS = 8
PER_THREAD = 5
JOIN_TIMEOUT_S = 120


@pytest.fixture
def pg_ctx(postgres_url: str, tmp_path: Path) -> Iterator[AppContext]:
    """A migrated, config-synced context on a fresh Postgres database."""
    _, config_dir, data_dir, _ = make_project_dirs(tmp_path)
    settings = Settings(config_dir=config_dir, data_dir=data_dir, database_url=postgres_url)
    ctx = AppContext.from_settings(settings)
    upgrade_to_head(ctx.engine)
    ctx.sync_config()
    set_context(ctx)
    yield ctx
    set_context(None)
    ctx.dispose()


def _reg(ctx: AppContext, path: str, hash: str = "h") -> models.RegisterResult:
    with ctx.write_session() as session:
        return models.register_file(
            session, project=PROJECT, storage_location="primary", path=path, hash=hash, size=1
        )


def _remove(ctx: AppContext, file_id: int) -> None:
    with ctx.write_session() as session:
        _, copies = models.begin_delete(session, file_id, updated_by="carol")
    with ctx.write_session() as session:
        models.finish_delete(session, file_id, copies, updated_by="carol")


def _run_threads(target: Callable[[int], None], n: int) -> None:
    errors: list[BaseException] = []
    start = threading.Barrier(n)

    def run(t: int) -> None:
        try:
            start.wait()
            target(t)
        except BaseException as e:  # reported below
            errors.append(e)

    workers = [threading.Thread(target=run, args=(t,)) for t in range(n)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(JOIN_TIMEOUT_S)
    assert not any(w.is_alive() for w in workers), "a worker is stuck (deadlock?)"
    assert not errors, errors


def _check_series(ctx: AppContext, series_key: str) -> list[File]:
    """Assert the series invariants; return its live files."""
    with ctx.sessionmaker() as session:
        series = session.scalars(select(Series).where(Series.series_key == series_key)).one()
        live = list(
            session.scalars(
                select(File).where(File.series_id == series.id, File.deleted_at.is_(None))
            )
        )
        latest = [f for f in live if f.is_latest]
        assert len(latest) == 1
        assert series.latest_file_id == latest[0].id
        paths = [f.path for f in live]
        assert len(paths) == len(set(paths))
        deleted_latest = session.scalar(
            select(func.count(File.id)).where(File.deleted_at.is_not(None), File.is_latest)
        )
        assert deleted_latest == 0
        return live


@pytest.mark.req("DB-02")
def test_concurrent_adds_and_removes_in_one_series(pg_ctx: AppContext) -> None:
    def worker(t: int) -> None:
        for i in range(PER_THREAD):
            version = t * PER_THREAD + i + 1
            result = _reg(pg_ctx, f"cc/x_v{version:03d}.dat", hash=f"h{version}")
            _reg(pg_ctx, "cc/x_v999.dat", hash="same")  # every thread: one live row only
            if i % 2 == 0:
                _remove(pg_ctx, result.file_id)

    _run_threads(worker, THREADS)

    live = _check_series(pg_ctx, "v:cc/x_v{v}.dat")
    [latest] = [f for f in live if f.is_latest]
    assert latest.path == "cc/x_v999.dat"
    expected = {
        f"cc/x_v{t * PER_THREAD + i + 1:03d}.dat"
        for t in range(THREADS)
        for i in range(PER_THREAD)
        if i % 2
    } | {"cc/x_v999.dat"}
    assert {f.path for f in live} == expected


@pytest.mark.req("DB-02")
@pytest.mark.req("SVC-07")
def test_concurrent_delete_tasks_leave_the_previous_version_latest(pg_ctx: AppContext) -> None:
    ids = [_reg(pg_ctx, f"rr/x_v{v}.dat").file_id for v in range(1, 11)]
    to_remove = ids[2:]  # v3..v10, from four threads, through the delete_file task

    def worker(t: int) -> None:
        for file_id in to_remove[t::4]:
            delete_file.fn(file_id, user="carol")  # objects are absent: counts as deleted

    _run_threads(worker, 4)

    live = _check_series(pg_ctx, "v:rr/x_v{v}.dat")
    assert sorted(f.path for f in live) == ["rr/x_v1.dat", "rr/x_v2.dat"]
    assert [f.path for f in live if f.is_latest] == ["rr/x_v2.dat"]


@pytest.mark.req("DB-02")
def test_concurrent_adds_of_a_new_series_create_it_once(pg_ctx: AppContext) -> None:
    _run_threads(lambda t: _reg(pg_ctx, f"new/y_v{t + 1}.dat", hash=f"h{t}"), THREADS)

    live = _check_series(pg_ctx, "v:new/y_v{v}.dat")
    assert len(live) == THREADS
    assert [f.path for f in live if f.is_latest] == [f"new/y_v{THREADS}.dat"]
    with pg_ctx.sessionmaker() as session:
        count = session.scalar(
            select(func.count(Series.id)).where(Series.series_key == "v:new/y_v{v}.dat")
        )
        assert count == 1

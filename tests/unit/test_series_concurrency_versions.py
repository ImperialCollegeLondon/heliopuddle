"""Series: concurrent changes and version kinds."""

from __future__ import annotations

import threading
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from heliopuddle.server import models
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import File, Series
from tests.unit._series_helpers import (  # noqa: F401
    RULES,
    _stored,
    latest_paths,
    reg,
    remove,
)


@pytest.mark.req("DB-02")
def test_concurrent_adds_and_removes_in_one_series(ctx: AppContext) -> None:
    """Threads, each with its own session, add and remove versions of one series."""
    assert ctx.engine.url.database and ctx.engine.url.database != ":memory:"
    threads_n, per_thread = 6, 5
    errors: list[BaseException] = []
    start = threading.Barrier(threads_n)

    def worker(t: int) -> None:
        try:
            start.wait()
            for i in range(per_thread):
                version = t * per_thread + i + 1
                result = reg(ctx, f"cc/x_v{version:03d}.dat", hash=f"h{version}")
                # Every thread also adds the same path: only one live row may exist.
                reg(ctx, "cc/x_v999.dat", hash="same")
                if i % 2 == 0:
                    remove(ctx, result.file_id)
        except BaseException as e:  # pragma: no cover - reported below
            errors.append(e)

    workers = [threading.Thread(target=worker, args=(t,)) for t in range(threads_n)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=120)
    assert not errors, errors

    with ctx.sessionmaker() as session:
        series = session.scalars(select(Series).where(Series.series_key == "v:cc/x_v{v}.dat")).one()
        live = session.scalars(
            select(File).where(File.series_id == series.id, File.deleted_at.is_(None))
        ).all()
        latest = [f for f in live if f.is_latest]
        assert len(latest) == 1
        assert latest[0].path == "cc/x_v999.dat"
        assert series.latest_file_id == latest[0].id
        paths = [f.path for f in live]
        assert len(paths) == len(set(paths))
        expected_live = {
            f"cc/x_v{t * per_thread + i + 1:03d}.dat"
            for t in range(threads_n)
            for i in range(per_thread)
            if i % 2
        } | {"cc/x_v999.dat"}
        assert set(paths) == expected_live
        deleted_latest = session.scalar(
            select(func.count(File.id)).where(File.deleted_at.is_not(None), File.is_latest)
        )
        assert deleted_latest == 0


@pytest.mark.req("DB-02")
def test_concurrent_removals_leave_previous_latest(ctx: AppContext) -> None:
    ids = [reg(ctx, f"rr/x_v{v}.dat").file_id for v in range(1, 9)]
    errors: list[BaseException] = []
    start = threading.Barrier(4)

    def worker(chunk: list[int]) -> None:
        try:
            start.wait()
            for file_id in chunk:
                remove(ctx, file_id)
        except BaseException as e:  # pragma: no cover - reported below
            errors.append(e)

    # Remove v3..v8 from four threads; v2 must end up latest.
    to_remove = ids[2:]
    workers = [threading.Thread(target=worker, args=(to_remove[i::4],)) for i in range(4)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=120)
    assert not errors, errors
    assert latest_paths(ctx, "rr/x_v{v}.dat") == (["rr/x_v2.dat"], "rr/x_v2.dat")


def _version(ctx: AppContext, file_id: int) -> tuple[int | None, str | None, str | None]:
    with ctx.sessionmaker() as session:
        f = session.get_one(File, file_id)
        return f.version_number, f.version_text, f.series.version_type


@pytest.mark.req("API-20")
def test_override_replaces_the_derived_version_of_a_new_file(ctx: AppContext) -> None:
    # The series key still comes from the path; the version from the override.
    a = reg(ctx, "o/a_v3.cdf", version_number=12).file_id
    assert _version(ctx, a) == (12, None, "number")
    b = reg(ctx, "o/a_v4.cdf").file_id
    assert latest_paths(ctx, "o/a_v{v}.cdf") == (["o/a_v3.cdf"], "o/a_v3.cdf")
    # A path that matches no rule is versioned in its literal series.
    c = reg(ctx, "o/notes.txt", version_text="001.0002").file_id
    assert _version(ctx, c) == (None, "001.0002", "text")
    with ctx.sessionmaker() as session:
        assert session.get_one(File, c).series.series_key == "p:o/notes.txt"
        assert session.get_one(File, a).series_id == session.get_one(File, b).series_id
    with pytest.raises(ValueError, match="not both"):
        reg(ctx, "o/x_v1.cdf", version_number=1, version_text="1")


@pytest.mark.req("API-21")
def test_a_series_holds_one_kind_of_version(ctx: AppContext) -> None:
    text = reg(ctx, "calibration/layers/x_v1.2.json").file_id
    assert _version(ctx, text)[2] == "text"
    with pytest.raises(models.VersionKindMismatch, match="holds text versions") as e:
        reg(ctx, "calibration/layers/x_v1.3.json", version_number=5)
    assert (e.value.series_type, e.value.version_type) == ("text", "number")
    number = reg(ctx, "k/a_v1.cdf").file_id
    with pytest.raises(models.VersionKindMismatch, match="holds number versions"):
        reg(ctx, "k/a_v2.cdf", version_text="002")
    # The same kind, overridden, is fine; the kind never changes.
    reg(ctx, "k/a_v3.cdf", version_number=30)
    assert _version(ctx, number)[2] == "number"
    with ctx.sessionmaker() as session:
        assert session.scalar(select(func.count(File.id))) == 3


@pytest.mark.req("API-21")
def test_unversioned_files_fit_any_series_and_rank_lowest(ctx: AppContext) -> None:
    # A literal-path series: first unversioned, then re-added with a version.
    plain = reg(ctx, "u/readme.txt").file_id
    assert _version(ctx, plain) == (None, None, None)
    remove(ctx, plain)
    versioned = reg(ctx, "u/readme.txt", "h2", version_text="b").file_id
    assert _version(ctx, versioned) == (None, "b", "text")
    with ctx.sessionmaker() as session:
        series = session.get_one(File, versioned).series
        _, _, files = models.list_versions(session, series.id, include_deleted=True)
        assert [f.id for f in files] == [versioned, plain]


@pytest.mark.req("API-22")
def test_a_live_file_keeps_its_version(ctx: AppContext) -> None:
    first = reg(ctx, "i/a_v3.cdf", "abc")
    # No override, or an equal one: unchanged.
    assert reg(ctx, "i/a_v3.cdf", "abc").outcome == models.RegisterOutcome.UNCHANGED
    same = reg(ctx, "i/a_v3.cdf", "abc", version_number=3)
    assert same.outcome == models.RegisterOutcome.UNCHANGED
    for override in ({"version_number": 4}, {"version_text": "3"}):
        with pytest.raises(models.VersionImmutable, match="number 3") as e:
            reg(ctx, "i/a_v3.cdf", "abc", **override)
        assert e.value.file_id == first.file_id
    text = reg(ctx, "i/notes.txt", version_text="x").file_id
    reg(ctx, "i/notes.txt", version_text="x")
    with pytest.raises(models.VersionImmutable, match="text 'x'"):
        reg(ctx, "i/notes.txt", version_text="y")
    # A metadata or content-date change never touches the version.
    with ctx.write_session() as session:
        models.update_metadata(session, text, {"a": 1})
        models.set_content_date(session, text, datetime(2024, 1, 1, tzinfo=UTC), rules=RULES)
        models.set_content_date(session, first.file_id, None, rules=RULES)
    assert _version(ctx, text) == (None, "x", "text")
    assert _version(ctx, first.file_id) == (3, None, "number")


@pytest.mark.req("API-02")
def test_python_ordering_matches_the_series_kind(ctx: AppContext) -> None:
    def file(number: int | None, text: str | None, i: int) -> File:
        return File(
            id=i,
            version_number=number,
            version_text=text,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )

    texts = [file(None, "001.0002", 1), file(None, None, 2), file(None, "001.0010", 3)]
    assert [f.id for f in models.sort_versions(texts, "text")] == [3, 1, 2]
    numbers = [file(9, None, 1), file(None, None, 2), file(10, None, 3), file(10, None, 4)]
    assert [f.id for f in models.sort_versions(numbers, "number")] == [4, 3, 1, 2]
    assert [f.id for f in models.sort_versions(numbers, None)] == [4, 3, 2, 1]

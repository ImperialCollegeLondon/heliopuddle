"""Rule age criteria in selection and incremental crossings (SVC-04, SVC-05)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from heliopuddle.config.models import RuleMatch
from heliopuddle.server.context import AppContext
from heliopuddle.server.models.rules import age_crossings, select_candidates
from heliopuddle.server.schemas.rules import RuleCursor
from tests.unit._rule_engine_helpers import NOW, seed

DAY = timedelta(days=1)
HOUR = timedelta(hours=1)

# NOW is 2026-10-30T12:00Z. Each file's dates, as an age back from NOW:
#   name  mtime   created  content_date  arrived  is_latest
#   a     40d     40d      40d (09-20)   40d      false
#   b     5d      40d      5d  (10-25)   40d      true
#   c     -       20d      -             2d       true    (no mtime, no content date)
#   d     60d     10d      100d (07-22)  12h      false
#   e     1h      1h       45d (09-15)   1h       true
DATASET: dict[str, dict[str, Any]] = {
    "a": {"mtime": 40 * DAY, "created": 40 * DAY, "content": 40 * DAY, "arrived": 40 * DAY},
    "b": {"mtime": 5 * DAY, "created": 40 * DAY, "content": 5 * DAY, "arrived": 40 * DAY},
    "c": {"mtime": None, "created": 20 * DAY, "content": None, "arrived": 2 * DAY},
    "d": {"mtime": 60 * DAY, "created": 10 * DAY, "content": 100 * DAY, "arrived": 12 * HOUR},
    "e": {"mtime": HOUR, "created": HOUR, "content": 45 * DAY, "arrived": HOUR},
}
LATEST = {"a": False, "b": True, "c": True, "d": False, "e": True}
ALL = set(DATASET)


def _seed_dataset(ctx: AppContext) -> dict[str, int]:
    def at(age: timedelta | None) -> Any:
        return None if age is None else NOW - age

    return {
        name: seed(
            ctx,
            f"data/{name}.dat",
            created_at=at(dates["created"]),
            content_date=at(dates["content"]),
            is_latest=LATEST[name],
            copy={"mtime": at(dates["mtime"]), "arrived_at": at(dates["arrived"])},
        )
        for name, dates in DATASET.items()
    }


def _age(**fields: dict[str, str]) -> dict[str, Any]:
    return {"paths": ["**"], "age": fields}


GOLDEN: list[tuple[str, dict[str, Any], set[str]]] = [
    # modified: the source copy's mtime, else when hp first recorded the file
    ("modified older", _age(modified={"older_than": "30d"}), {"a", "d"}),
    ("modified younger", _age(modified={"younger_than": "30d"}), {"b", "c", "e"}),
    ("modified falls back to created", _age(modified={"older_than": "15d"}), {"a", "c", "d"}),
    (
        "modified range",
        _age(modified={"older_than": "10d", "younger_than": "50d"}),
        {"a", "c"},
    ),
    ("modified in weeks", _age(modified={"older_than": "4w"}), {"a", "d"}),
    # created: when hp first recorded the file
    ("created older", _age(created={"older_than": "30d"}), {"a", "b"}),
    ("created younger", _age(created={"younger_than": "15d"}), {"d", "e"}),
    ("created older absolute", _age(created={"older_than": "2026-10-01"}), {"a", "b"}),
    # content_date: a missing date never matches
    ("content older", _age(content_date={"older_than": "30d"}), {"a", "d", "e"}),
    ("content younger", _age(content_date={"younger_than": "30d"}), {"b"}),
    (
        "content range",
        _age(content_date={"older_than": "30d", "younger_than": "60d"}),
        {"a", "e"},
    ),
    ("content any date", _age(content_date={"younger_than": "10y"}), {"a", "b", "d", "e"}),
    ("content older absolute", _age(content_date={"older_than": "2026-09-25"}), {"a", "d", "e"}),
    ("content younger absolute", _age(content_date={"younger_than": "2026-09-18"}), {"a", "b"}),
    (
        "content absolute range",
        _age(content_date={"older_than": "2026-09-25", "younger_than": "2026-09-18"}),
        {"a"},
    ),
    (
        "older_than is strictly before",
        _age(content_date={"older_than": "2026-09-20T12:00:00"}),
        {"d", "e"},
    ),
    (
        "younger_than is at or after",
        _age(content_date={"younger_than": "2026-09-20T12:00:00Z"}),
        {"a", "b"},
    ),
    (
        "content mixed duration and date",
        _age(content_date={"older_than": "30d", "younger_than": "2026-09-18"}),
        {"a"},
    ),
    # arrived: when the file arrived in the source storage location
    ("arrived older", _age(arrived={"older_than": "1d"}), {"a", "b", "c"}),
    ("arrived younger", _age(arrived={"younger_than": "1d"}), {"d", "e"}),
    ("arrived range", _age(arrived={"older_than": "6h", "younger_than": "1d"}), {"d"}),
    ("arrived older absolute", _age(arrived={"older_than": "2026-10-30"}), {"a", "b", "c"}),
    # combinations: every criterion listed must hold
    (
        "modified and arrived",
        _age(modified={"older_than": "30d"}, arrived={"younger_than": "1d"}),
        {"d"},
    ),
    (
        "created and content",
        _age(created={"older_than": "30d"}, content_date={"younger_than": "30d"}),
        {"b"},
    ),
    (
        "all four",
        _age(
            modified={"older_than": "1d"},
            created={"younger_than": "30d"},
            content_date={"older_than": "30d"},
            arrived={"younger_than": "1d"},
        ),
        {"d"},
    ),
    # is_latest
    ("every version", {"paths": ["**"], "is_latest": None}, ALL),
    ("latest only", {"paths": ["**"], "is_latest": True}, {"b", "c", "e"}),
    ("all but latest", {"paths": ["**"], "is_latest": False}, {"a", "d"}),
    (
        "latest and arrived",
        {"paths": ["**"], "is_latest": True, "age": {"arrived": {"older_than": "1d"}}},
        {"b", "c"},
    ),
    (
        "not latest and content",
        {"paths": ["**"], "is_latest": False, "age": {"content_date": {"younger_than": "60d"}}},
        {"a"},
    ),
]


@pytest.mark.req("SVC-04")
@pytest.mark.parametrize(("match", "expected"), [g[1:] for g in GOLDEN], ids=[g[0] for g in GOLDEN])
def test_golden_age_selection(ctx: AppContext, match: dict[str, Any], expected: set[str]) -> None:
    ids = _seed_dataset(ctx)
    rule = RuleMatch.model_validate(match)
    with ctx.sessionmaker() as session:
        full = select_candidates(session, 1, 1, rule, now=NOW)
        by_id = select_candidates(session, 1, 1, rule, now=NOW, after_id=0)
    assert {c.file_id for c in full} == {ids[name] for name in expected}
    assert {c.file_id for c in by_id} == {ids[name] for name in expected}


# --- incremental crossings (SVC-05) --------------------------------------------------

SINCE = NOW - 2 * DAY


def _incremental(ctx: AppContext, match: dict[str, Any]) -> set[int]:
    """Files an incremental run at NOW selects; nothing changed since SINCE."""
    rule = RuleMatch.model_validate(match)
    with ctx.sessionmaker() as session:
        return {
            c.file_id
            for c in select_candidates(
                session,
                1,
                1,
                rule,
                now=NOW,
                changed_after=RuleCursor(updated_at=SINCE, id=0),
                age_since=SINCE,
            )
        }


def _full(ctx: AppContext, match: dict[str, Any]) -> set[int]:
    with ctx.sessionmaker() as session:
        return {
            c.file_id
            for c in select_candidates(session, 1, 1, RuleMatch.model_validate(match), now=NOW)
        }


@pytest.mark.req("SVC-05")
def test_incremental_selects_arrival_crossings(ctx: AppContext) -> None:
    crossed = seed(ctx, "crossed.dat", copy={"arrived_at": NOW - 36 * HOUR})
    already = seed(ctx, "already.dat", copy={"arrived_at": NOW - 5 * DAY})
    seed(ctx, "cooling.dat", copy={"arrived_at": NOW - 12 * HOUR})
    match = _age(arrived={"older_than": "1d"})
    assert _incremental(ctx, match) == {crossed}
    assert _full(ctx, match) == {crossed, already}


@pytest.mark.req("SVC-05")
def test_incremental_selects_content_date_crossings(ctx: AppContext) -> None:
    crossed = seed(ctx, "crossed.dat", content_date=NOW - 31 * DAY)
    already = seed(ctx, "already.dat", content_date=NOW - 40 * DAY)
    seed(ctx, "undated.dat", content_date=None)
    seed(ctx, "recent.dat", content_date=NOW - 10 * DAY)
    match = _age(content_date={"older_than": "30d"})
    assert _incremental(ctx, match) == {crossed}
    assert _full(ctx, match) == {crossed, already}


@pytest.mark.req("SVC-05")
def test_a_crossing_on_any_field_selects_when_every_criterion_holds(ctx: AppContext) -> None:
    by_arrival = seed(ctx, "arrival.dat", copy={"arrived_at": NOW - 36 * HOUR})
    by_mtime = seed(ctx, "mtime.dat", copy={"mtime": NOW - 31 * DAY})
    seed(ctx, "neither.dat")  # both long past their thresholds
    seed(ctx, "cooling.dat", copy={"mtime": NOW - 31 * DAY, "arrived_at": NOW - HOUR})
    match = _age(modified={"older_than": "30d"}, arrived={"older_than": "1d"})
    assert _incremental(ctx, match) == {by_arrival, by_mtime}


@pytest.mark.req("SVC-05")
@pytest.mark.parametrize(
    "match",
    [
        _age(arrived={"younger_than": "1d"}),
        _age(content_date={"older_than": "2026-10-30"}),
        _age(modified={"younger_than": "2026-10-01"}),
    ],
    ids=["younger_than", "absolute older_than", "absolute younger_than"],
)
def test_younger_than_and_absolute_bounds_produce_no_crossings(
    ctx: AppContext, match: dict[str, Any]
) -> None:
    seed(
        ctx,
        "recent.dat",
        content_date=NOW - 36 * HOUR,
        copy={"arrived_at": NOW - 12 * HOUR, "mtime": NOW - 36 * HOUR},
    )
    assert age_crossings(RuleMatch.model_validate(match), SINCE) is None
    assert _full(ctx, match)  # the file matches...
    assert _incremental(ctx, match) == set()  # ...but did not change or cross


@pytest.mark.req("SVC-05")
def test_a_range_crosses_on_its_relative_older_than_bound(ctx: AppContext) -> None:
    crossed = seed(ctx, "crossed.dat", copy={"arrived_at": NOW - 36 * HOUR})
    seed(ctx, "too-old.dat", copy={"arrived_at": NOW - 5 * DAY})
    match = _age(arrived={"older_than": "1d", "younger_than": "3d"})
    assert _incremental(ctx, match) == {crossed}

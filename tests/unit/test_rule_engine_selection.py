"""Rule engine: names, selection and basic runs."""

from __future__ import annotations

import logging
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

from heliopuddle.config.models import (
    BatchConfig,
    RuleMatch,
    RuleSpec,
)
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import RuleState, StorageLocation
from heliopuddle.server.schemas import FileState
from heliopuddle.server.schemas.rules import RuleCursor
from tests.unit._rule_engine_helpers import (
    NOW,
    OLD,
    configured,
    engine,
    seed,
)

_FIXTURES = (engine,)  # referenced so ruff sees the imports used


@pytest.mark.req("SVC-21")
def test_rule_names_disambiguate_projects_with_hyphens() -> None:
    from heliopuddle.server.services.rule_names import (
        RULE_ONCE_DEPLOYMENT,
        rule_deployment_full_name,
        rule_deployment_name,
    )

    left = rule_deployment_full_name(1, "alpha-beta", "archive")
    right = rule_deployment_full_name(2, "alpha", "beta-archive")
    assert left != right
    assert left == "run_rule/rule-p1-alpha-beta-archive"
    assert rule_deployment_name(1, "alpha-beta", "archive") == left.split("/")[1]
    assert RULE_ONCE_DEPLOYMENT == "run_rule_once/rule-once"


@pytest.mark.req("SVC-04")
@pytest.mark.parametrize(
    ("match", "expected"),
    [
        ({"paths": ["data/**/*.dat"]}, {"old", "new", "nested", "no-date", "no-mtime"}),
        ({"paths": ["data/*.dat"], "exclude": ["**/new*"]}, {"old", "no-date", "no-mtime"}),
        ({"paths": ["**"], "is_latest": False}, {"old"}),
        (
            {"paths": ["**"], "age": {"modified": {"older_than": "30d"}}},
            {"old", "nested", "no-date", "no-mtime", "literal"},
        ),
        (
            {"paths": ["**"], "age": {"content_date": {"older_than": "30d"}}},
            {"old", "nested", "no-mtime", "literal"},
        ),
        ({"paths": ["literal%_/**/*.dat"]}, {"literal"}),
        ({"paths": ["literalXX/**/*.dat"]}, set()),
    ],
)
def test_golden_rule_selection(ctx: AppContext, match: dict[str, Any], expected: set[str]) -> None:
    from heliopuddle.server.models.rules import select_candidates

    ids = {
        "old": seed(ctx, "data/old.dat", is_latest=False),
        "new": seed(
            ctx,
            "data/new.dat",
            content_date=NOW - timedelta(days=5),
            copy={"mtime": NOW - timedelta(days=5)},
        ),
        "nested": seed(ctx, "data/deep/nested.dat"),
        "no-date": seed(ctx, "data/no-date.dat", content_date=None),
        "no-mtime": seed(ctx, "data/no-mtime.dat", copy={"mtime": None}),
        "literal": seed(ctx, "literal%_/one.dat"),
    }
    seed(ctx, "data/ignored.dat", ignored_from_all_rules=True)
    seed(ctx, "data/dead.dat", deleted_at=NOW)
    seed(ctx, "data/unknown.dat", copy={"state": FileState.UNKNOWN})
    seed(ctx, "data/owned.dat", copy={"op_token": "held", "op_deadline": NOW + timedelta(hours=1)})
    seed(ctx, "data/expired.dat", copy={"op_token": "expired", "op_deadline": OLD})
    with ctx.sessionmaker() as session:
        found = select_candidates(session, 1, 1, RuleMatch.model_validate(match), now=NOW)
    assert {candidate.file_id for candidate in found} == {ids[name] for name in expected}

    with ctx.write_session() as session:
        session.get_one(StorageLocation, 1).active = False
    with ctx.sessionmaker() as session:
        assert select_candidates(session, 1, 1, RuleMatch(paths=["**"]), now=NOW) == []


@pytest.mark.req("SVC-05")
def test_incremental_file_copy_latest_and_age_changes(ctx: AppContext) -> None:
    from heliopuddle.server.models.rules import select_candidates

    since = NOW - timedelta(days=2)
    unchanged = seed(ctx, "unchanged.dat")
    file_change = seed(ctx, "file.dat", updated_at=NOW - timedelta(days=1))
    copy_change = seed(ctx, "copy.dat", copy={"updated_at": NOW - timedelta(hours=1)})
    crossing = seed(ctx, "crossing.dat", copy={"mtime": NOW - timedelta(days=31)})
    missing_date = seed(ctx, "undated.dat", content_date=None)
    match = RuleMatch.model_validate({"paths": ["**"], "age": {"modified": {"older_than": "30d"}}})
    with ctx.sessionmaker() as session:
        found = select_candidates(
            session,
            1,
            1,
            match,
            now=NOW,
            changed_after=RuleCursor(updated_at=since, id=0),
            age_since=since,
        )
        assert {candidate.file_id for candidate in found} == {file_change, copy_change, crossing}
        assert {
            candidate.file_id for candidate in select_candidates(session, 1, 1, match, now=NOW)
        } == {unchanged, file_change, copy_change, crossing, missing_date}
        affected = select_candidates(session, 1, 1, match, now=NOW, file_ids=[copy_change])
        assert [candidate.file_id for candidate in affected] == [copy_change]
        assert affected[0].cursor.updated_at == NOW - timedelta(hours=1)


@pytest.mark.req("SVC-22")
@pytest.mark.slow
def test_selection_keysets_cross_ties_and_exact_glob_rejections(ctx: AppContext) -> None:
    from heliopuddle.server.models.rules import select_candidates

    expected = [seed(ctx, f"keep/{i:03}.dat") for i in range(250)]
    for i in range(10):
        seed(ctx, f"keep/excluded/{i}.dat")
    cursor = None
    found = []
    for count in (100, 100, 50):
        with ctx.sessionmaker() as session:
            page = select_candidates(
                session,
                1,
                1,
                RuleMatch(paths=["keep/*.dat"]),
                now=NOW,
                after=cursor,
                limit=100,
            )
        assert len(page) == count
        found.extend(candidate.file_id for candidate in page)
        cursor = page[-1].cursor
    assert found == expected


@pytest.mark.req("SVC-05", "SVC-21", "SVC-22", "SVC-23")
@pytest.mark.slow
def test_configured_rule_three_batches_with_tied_updates(
    ctx: AppContext, engine: SimpleNamespace
) -> None:
    configured(ctx)
    expected = [seed(ctx, f"{i:03}.dat") for i in range(250)]
    params: dict[str, Any] = {"project": "demo", "rule": "archive"}
    for count in (100, 100, 50):
        result = engine.module.run_rule.fn(**params)
        assert result.processed == count
        if engine.successors:
            params = engine.successors.pop(0)
    assert engine.calls == expected
    assert engine.successors == []
    assert engine.locks == ["rule:demo:archive"] * 3
    with ctx.sessionmaker() as session:
        state = session.get_one(RuleState, (1, "archive"))
        assert state.watermark_id == expected[-1]
        assert state.last_success_at == NOW


@pytest.mark.req("SVC-23")
@pytest.mark.parametrize(
    ("limits", "expected"),
    [
        ({"max_files": 2, "max_bytes": None, "max_duration": None}, 2),
        ({"max_files": None, "max_bytes": 15, "max_duration": None}, 2),
        ({"max_files": 3, "max_bytes": 1, "max_duration": "30m"}, 1),
    ],
)
def test_rule_stops_after_first_reached_limit_and_manual_run_resumes(
    ctx: AppContext, engine: SimpleNamespace, limits: dict[str, Any], expected: int
) -> None:
    configured(ctx, batch=limits, retrigger=False)
    ids = [seed(ctx, f"{i}.dat") for i in range(5)]
    assert engine.module.run_rule.fn("demo", "archive").processed == expected
    assert engine.calls == ids[:expected]
    assert engine.successors == []
    engine.module.run_rule.fn("demo", "archive")
    assert engine.calls == ids[: expected * 2]


@pytest.mark.req("SVC-21")
def test_dry_run_and_one_off_never_write_rule_state(
    ctx: AppContext, engine: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    configured(ctx)
    seed(ctx, "one.dat")
    with caplog.at_level(logging.INFO):
        result = engine.module.run_rule.fn("demo", "archive", dry_run=True)
    assert result.processed == 1
    assert "would_copy" in caplog.text
    assert engine.calls == []
    with ctx.sessionmaker() as session:
        assert session.scalars(select(RuleState)).all() == []
    spec = RuleSpec(
        action="copy",
        source="primary",
        destination="secondary",
        match=RuleMatch(paths=["**"]),
    )
    spec.options.batch = BatchConfig(max_files=1)
    seed(ctx, "two.dat")
    engine.module.run_rule_once.fn("demo", spec)
    assert len(engine.calls) == 1
    assert engine.successors == []
    with ctx.sessionmaker() as session:
        assert session.scalars(select(RuleState)).all() == []

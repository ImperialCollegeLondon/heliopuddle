"""Rule engine: limits, continuations and checkpoints."""

from __future__ import annotations

from contextlib import nullcontext
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import update

from heliopuddle.config.models import (
    AgeCriteria,
    AgeRange,
)
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import File, RuleState
from heliopuddle.server.services import rule_checkpoint
from tests.unit._rule_engine_helpers import (  # noqa: F401
    NOW,
    OLD,
    configured,
    engine,
    seed,
)

_FIXTURES = (engine,)  # referenced so ruff sees the imports used


@pytest.mark.req("SVC-21", "SVC-23")
def test_failed_file_does_not_advance_past_failed_work(
    ctx: AppContext, engine: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured(ctx, retrigger=False)
    ids = [seed(ctx, f"{i}.dat") for i in range(3)]
    original = engine.module._execute_file

    def fail(candidate, spec, **kwargs):
        if candidate.file_id == ids[1]:
            raise RuntimeError("store unavailable")
        return original(candidate, spec, **kwargs)

    monkeypatch.setattr(engine.module, "_execute_file", fail)
    with pytest.raises(RuntimeError, match="failed"):
        engine.module.run_rule.fn("demo", "archive")
    with ctx.sessionmaker() as session:
        state = session.get(RuleState, (1, "archive"))
        assert state is None or state.watermark_id in (None, ids[0])
    assert engine.successors == []
    monkeypatch.setattr(engine.module, "_execute_file", original)
    engine.module.run_rule.fn("demo", "archive")
    assert ids[1] in engine.calls


@pytest.mark.req("SVC-21", "SVC-23")
def test_disabled_rules_do_no_work_or_retrigger(ctx: AppContext, engine: SimpleNamespace) -> None:
    configured(ctx)
    seed(ctx, "one.dat")
    with ctx.write_session() as session:
        session.add(RuleState(project_id=1, rule_name="archive", enabled=False))
    assert engine.module.run_rule.fn("demo", "archive").disabled
    assert engine.calls == engine.successors == []


@pytest.mark.req("SVC-05", "SVC-23")
def test_late_arrival_is_requeried_and_full_does_not_recopy_own_updates(
    ctx: AppContext, engine: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured(ctx, batch={"max_files": 2})
    original_ids = [seed(ctx, f"{i}.dat") for i in range(2)]
    original = engine.module._execute_file
    late: list[int] = []

    def during_transfer(candidate, spec, **kwargs):
        result = original(candidate, spec, **kwargs)
        with ctx.write_session() as session:
            session.execute(
                update(File)
                .where(File.id == candidate.file_id)
                .values(updated_at=NOW + timedelta(seconds=1))
            )
        if not late:
            late.append(seed(ctx, "late.dat", updated_at=NOW + timedelta(seconds=1)))
        return result

    monkeypatch.setattr(engine.module, "_execute_file", during_transfer)
    engine.module.run_rule.fn("demo", "archive", mode="full")
    assert engine.calls == original_ids
    assert len(engine.successors) == 1
    engine.module.run_rule.fn(**engine.successors.pop(0))
    assert engine.calls == [*original_ids, *late]
    assert engine.successors == []


@pytest.mark.req("SVC-23")
def test_rule_duration_limit_finishes_current_file(
    ctx: AppContext, engine: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from heliopuddle.server.services import batches

    configured(ctx, batch={"max_files": None, "max_bytes": None, "max_duration": "3s"})
    for i in range(5):
        seed(ctx, f"{i}.dat")
    clock = [0.0]
    monkeypatch.setattr(batches, "monotonic", lambda: clock[0])
    original = engine.module._execute_file

    def slow(candidate, spec):
        clock[0] += 2
        return original(candidate, spec)

    monkeypatch.setattr(engine.module, "_execute_file", slow)
    assert engine.module.run_rule.fn("demo", "archive").processed == 2
    assert len(engine.successors) == 1


@pytest.mark.req("SVC-05", "SVC-22", "SVC-23")
def test_age_window_is_fixed_across_three_continuations(
    ctx: AppContext, engine: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    rule = configured(ctx)
    rule.match.age = AgeCriteria(modified=AgeRange(older_than=timedelta(days=30)))
    ids = [
        seed(ctx, f"aged/{i:03}.dat", copy={"mtime": NOW - timedelta(days=31)}) for i in range(250)
    ]
    seed(ctx, "later.dat", copy={"mtime": NOW - timedelta(days=20)})
    since = NOW - timedelta(days=2)
    with ctx.write_session() as session:
        session.add(
            RuleState(
                project_id=1,
                rule_name="archive",
                watermark_at=since,
                watermark_id=999,
                last_success_at=since,
            )
        )
    engine.module.run_rule.fn("demo", "archive")
    monkeypatch.setattr(engine.module, "utcnow", lambda: NOW + timedelta(hours=1))
    monkeypatch.setattr(rule_checkpoint, "utcnow", lambda: NOW + timedelta(hours=1))
    engine.module.run_rule.fn(**engine.successors.pop(0))
    engine.module.run_rule.fn(**engine.successors.pop(0))
    assert engine.calls == ids
    assert engine.successors == []
    with ctx.sessionmaker() as session:
        assert session.get_one(RuleState, (1, "archive")).last_success_at == NOW


@pytest.mark.req("SVC-21", "SVC-23")
def test_disable_during_active_batch_prevents_successor(
    ctx: AppContext, engine: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured(ctx, batch={"max_files": 2})
    for i in range(3):
        seed(ctx, f"{i}.dat")
    original = engine.module._execute_file

    def disable(candidate, spec):
        if not engine.calls:
            with ctx.write_session() as session:
                session.add(RuleState(project_id=1, rule_name="archive", enabled=False))
        return original(candidate, spec)

    monkeypatch.setattr(engine.module, "_execute_file", disable)
    assert engine.module.run_rule.fn("demo", "archive").processed == 2
    assert engine.successors == []
    with ctx.sessionmaker() as session:
        assert session.get_one(RuleState, (1, "archive")).enabled is False


@pytest.mark.req("SVC-05", "SVC-23")
@pytest.mark.parametrize("mode", ["full", "incremental"])
def test_prefect_checkpoint_resumes_paused_full_and_age_chains_after_restart(
    ctx: AppContext, engine: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    import prefect.client.orchestration
    from prefect.context import FlowRunContext

    rule = configured(ctx, batch={"max_files": 2}, retrigger=False)
    rule.match.age = AgeCriteria(modified=AgeRange(older_than=timedelta(days=30)))
    ids = [seed(ctx, f"{i}.dat", copy={"mtime": NOW - timedelta(days=31)}) for i in range(5)]
    since = NOW - timedelta(days=2)
    if mode == "incremental":
        with ctx.write_session() as session:
            session.add(
                RuleState(
                    project_id=1,
                    rule_name="archive",
                    watermark_at=since,
                    watermark_id=100,
                    last_success_at=since,
                )
            )
    history: list[SimpleNamespace] = []
    current: list[SimpleNamespace] = []

    def save(id, parameters, **kwargs):
        history.append(SimpleNamespace(id=id, parameters=parameters, **kwargs))

    client = SimpleNamespace(
        read_flow_runs=lambda **kw: list(reversed(history)),
        update_flow_run=save,
    )
    monkeypatch.setattr(
        prefect.client.orchestration, "get_client", lambda **kw: nullcontext(client)
    )
    monkeypatch.setattr(FlowRunContext, "get", lambda: current[0])
    for count in (2, 2, 1):
        # Also works for directly invoked Prefect flows, not only deployments.
        current[:] = [
            SimpleNamespace(
                flow_run=SimpleNamespace(
                    id=uuid4(),
                    deployment_id=None,
                    tags=[],
                )
            )
        ]
        assert engine.module.run_rule.fn("demo", "archive", mode=mode).processed == count
    assert engine.calls == ids
    assert len(history) == 3
    assert history[-1].parameters["continuation"]["pending"] is False
    assert engine.successors == []


@pytest.mark.req("SVC-21", "SVC-23")
def test_successor_rechecks_enablement_after_prefect_submission(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    import prefect.client.orchestration

    from heliopuddle.server.services import rules

    spec = configured(ctx)
    resolved = rules._resolve(ctx, "demo", spec, "archive")
    deployment_id, run_id = uuid4(), uuid4()
    submitted, cancelled = [], []

    def create(id, **kwargs):
        submitted.append((id, kwargs))
        with ctx.write_session() as session:
            session.add(RuleState(project_id=1, rule_name="archive", enabled=False))
        return SimpleNamespace(id=run_id)

    client = SimpleNamespace(
        read_deployment_by_name=lambda name: SimpleNamespace(id=deployment_id),
        create_flow_run_from_deployment=create,
        set_flow_run_state=lambda id, state, force: cancelled.append((id, state, force)),
    )
    monkeypatch.setattr(
        prefect.client.orchestration, "get_client", lambda **kw: nullcontext(client)
    )
    monkeypatch.setattr(rule_checkpoint, "utcnow", lambda: NOW)
    params = {"project": "demo", "rule": "archive", "continuation": {"chain_id": "test"}}
    rules._schedule_successor(ctx, resolved, parameters=params)
    assert submitted[0][0] == deployment_id
    assert submitted[0][1]["parameters"] == params
    assert submitted[0][1]["state"].state_details.scheduled_time == NOW + timedelta(seconds=30)
    assert cancelled[0][0] == run_id and cancelled[0][1].is_cancelled() and cancelled[0][2]
    rules._schedule_successor(ctx, resolved, parameters=params)
    assert len(submitted) == 1


@pytest.mark.req("SVC-22", "SVC-23")
def test_zero_pause_late_change_with_earlier_id_and_tied_timestamp_is_not_skipped(
    ctx: AppContext, engine: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured(ctx, pause_between_runs="0s")
    early = seed(ctx, "early.dat")
    late = seed(ctx, "late.dat", updated_at=NOW)
    since = NOW - timedelta(days=2)
    with ctx.write_session() as session:
        session.add(
            RuleState(
                project_id=1,
                rule_name="archive",
                watermark_at=since,
                watermark_id=0,
                last_success_at=since,
            )
        )
        session.get_one(File, early).updated_at = NOW - timedelta(days=1)
    original = engine.module._execute_file

    def touch_earlier_file(candidate, spec):
        result = original(candidate, spec)
        if candidate.file_id == late and len(engine.calls) == 2:
            with ctx.write_session() as session:
                session.get_one(File, early).updated_at = NOW
        return result

    monkeypatch.setattr(engine.module, "_execute_file", touch_earlier_file)
    engine.module.run_rule.fn("demo", "archive", mode="incremental")
    assert engine.calls == [early, late]
    assert len(engine.successors) == 1
    engine.module.run_rule.fn(**engine.successors.pop())
    assert early in engine.calls[2:]


@pytest.mark.req("SVC-04")
def test_file_ignored_after_the_page_was_read_is_not_processed(
    ctx: AppContext, engine: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file flagged ignored (or deleted) after its page was fetched is skipped, not run."""
    configured(ctx)
    ids = [seed(ctx, f"data/{name}.dat") for name in ("a", "b", "c")]

    def perform(candidate: Any, spec: Any, **kwargs: Any) -> Any:
        engine.calls.append(candidate.file_id)
        if len(engine.calls) == 1:
            with ctx.write_session() as session:
                session.get_one(File, ids[1]).ignored_from_all_rules = True
                session.get_one(File, ids[2]).deleted_at = NOW
        return SimpleNamespace(
            file_id=candidate.file_id, path=candidate.path, outcome="copied", detail=None
        )

    monkeypatch.setattr(engine.module, "_execute_file", perform)
    summary = engine.module.run_rule.fn("demo", "archive")
    assert engine.calls == [ids[0]]
    assert summary.processed == 1

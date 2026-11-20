"""Rule deployment lifecycle and preserved runtime enablement (SVC-21)."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from prefect import flow
from prefect.client.schemas.objects import ConcurrencyLimitStrategy

from heliopuddle.config.models import ProjectConfig
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import RuleState
from heliopuddle.server.services import deploy

pytestmark = pytest.mark.req("SVC-21")


@flow(name="run_rule")
def fake_rule(project: str, rule: str, mode: str = "incremental", dry_run: bool = False):
    return None


@flow(name="run_rule_once")
def fake_once(project: str, spec: dict[str, Any]):
    return None


@pytest.fixture
def rule_module(monkeypatch: pytest.MonkeyPatch) -> None:
    module = ModuleType("heliopuddle.server.services.rules")
    module.run_rule = fake_rule
    module.run_rule_once = fake_once
    monkeypatch.setitem(sys.modules, module.__name__, module)


def project() -> ProjectConfig:
    return ProjectConfig.model_validate(
        {
            "project": "demo",
            "storage_locations": [{"name": "primary", "url": "memory://demo"}],
            "rules": [
                {
                    "name": "scheduled",
                    "action": "delete",
                    "source": "primary",
                    "match": {"paths": ["**"]},
                    "schedule": "0 1 * * *",
                },
                {
                    "name": "on-demand",
                    "action": "delete",
                    "source": "primary",
                    "match": {"paths": ["**"]},
                },
            ],
        }
    )


def test_rule_deployments_have_explicit_params_schedule_and_cancel_new(rule_module) -> None:
    deps = deploy.build_deployments(
        projects={"demo": project()},
        ids={("demo", "primary"): (7, 8)},
        rule_enabled={(7, "scheduled"): False},
    )
    rules = {dep.name: dep for dep in deps if dep.flow_name == "run_rule"}
    assert set(rules) == {"rule-p7-demo-scheduled", "rule-p7-demo-on-demand"}
    scheduled = rules["rule-p7-demo-scheduled"]
    assert scheduled.parameters == {"project": "demo", "rule": "scheduled"}
    assert scheduled.paused is True
    assert len(scheduled.schedules) == 1
    assert scheduled.schedules[0].active is False
    assert scheduled.schedules[0].schedule.cron == "0 1 * * *"
    assert scheduled.concurrency_limit == 1
    assert scheduled.concurrency_options.collision_strategy == ConcurrencyLimitStrategy.ENQUEUE
    assert not rules["rule-p7-demo-on-demand"].schedules
    assert any(dep.flow_name == "run_rule_once" and dep.name == "rule-once" for dep in deps)


def test_duplicate_rule_identities_fail_before_build(rule_module) -> None:
    cfg = project()
    cfg.rules.append(cfg.rules[0])
    with pytest.raises(ValueError, match=r"collide|duplicate"):
        deploy.build_deployments(
            projects={"demo": cfg},
            ids={("demo", "primary"): (7, 8)},
        )


def test_startup_reads_disabled_state_without_reset(ctx: AppContext) -> None:
    ctx.projects["demo"].rules = project().rules
    with ctx.write_session() as session:
        session.add(RuleState(project_id=1, rule_name="scheduled", enabled=False))
    assert deploy.rule_enablement(ctx) == {(1, "scheduled"): False, (1, "on-demand"): True}
    ctx.sync_config()
    assert deploy.rule_enablement(ctx)[(1, "scheduled")] is False


def test_stale_rule_deployments_and_removed_schedules_are_deleted(rule_module) -> None:
    cfg = project()
    deps = deploy.build_deployments(
        projects={"demo": cfg},
        ids={("demo", "primary"): (7, 8)},
    )
    stale = SimpleNamespace(id=uuid4(), name="rule-p7-demo-stale", schedules=[])
    old_schedule = SimpleNamespace(id=uuid4(), slug=None)
    on_demand = SimpleNamespace(
        id=uuid4(),
        name="rule-p7-demo-on-demand",
        schedules=[old_schedule],
    )
    unrelated = SimpleNamespace(id=uuid4(), name="unrelated", schedules=[])
    removed, schedules, limits = [], [], []

    client = SimpleNamespace(
        upsert_global_concurrency_limit_by_name=lambda name, n: limits.append((name, n)),
        read_deployments=lambda **kw: [stale, on_demand, unrelated],
        delete_deployment=lambda id: removed.append(id),
        delete_deployment_schedule=lambda dep, id: schedules.append((dep, id)),
    )
    result = deploy.prepare_rule_services({"demo": cfg}, deps, client)
    assert removed == [stale.id]
    assert schedules == [(on_demand.id, old_schedule.id)]
    assert result == ["run_rule/rule-p7-demo-stale"]
    assert limits == [("rule:demo:scheduled", 1), ("rule:demo:on-demand", 1)]

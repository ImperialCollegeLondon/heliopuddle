"""Reconcile deployment lifecycle (SVC-27), without a Prefect server."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from prefect import flow
from prefect.client.schemas.objects import ConcurrencyLimitStrategy

from heliopuddle.config.models import ProjectConfig
from heliopuddle.server.services import deploy
from heliopuddle.server.services.rule_names import (
    reconcile_deployment_full_name,
    reconcile_deployment_name,
)

pytestmark = pytest.mark.req("SVC-27")


@flow(name="reconcile")
def fake_reconcile(project: str, verify: bool = False, rules: list[str] | None = None):
    return None


@pytest.fixture
def reconcile_module(monkeypatch: pytest.MonkeyPatch) -> None:
    module = ModuleType(deploy.RECONCILE_MODULE)
    module.reconcile = fake_reconcile  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)


def project(name: str = "demo", **reconcile: Any) -> ProjectConfig:
    return ProjectConfig.model_validate(
        {
            "project": name,
            "storage_locations": [{"name": "primary", "url": "memory://demo"}],
            "rules": [
                {
                    "name": "archive",
                    "action": "delete",
                    "source": "primary",
                    "match": {"paths": ["**"]},
                }
            ],
            **({"reconcile": reconcile} if reconcile else {}),
        }
    )


def reconciles(deps: list[Any]) -> dict[str, Any]:
    return {dep.name: dep for dep in deps if dep.flow_name == "reconcile"}


IDS = {("demo", "primary"): (7, 8)}


def test_names() -> None:
    assert reconcile_deployment_name(7, "demo") == "reconcile-p7-demo"
    assert reconcile_deployment_full_name(7, "demo") == "reconcile/reconcile-p7-demo"


def test_scheduled_deployment_has_params_cron_and_enqueue(reconcile_module) -> None:
    cfg = project(schedule="0 3 * * *", verify=True, rules=["archive"])
    deps = deploy.build_deployments(projects={"demo": cfg}, ids=IDS)
    dep = reconciles(deps)["reconcile-p7-demo"]
    assert dep.parameters == {"project": "demo", "verify": True, "rules": ["archive"]}
    assert len(dep.schedules) == 1
    assert dep.schedules[0].schedule.cron == "0 3 * * *"
    assert dep.schedules[0].slug == "reconcile"
    assert dep.concurrency_limit == 1
    assert dep.concurrency_options.collision_strategy == ConcurrencyLimitStrategy.ENQUEUE


def test_default_deployment_is_on_demand_with_defaults(reconcile_module) -> None:
    deps = deploy.build_deployments(projects={"demo": project()}, ids=IDS)
    dep = reconciles(deps)["reconcile-p7-demo"]
    assert not dep.schedules
    assert dep.parameters == {"project": "demo", "verify": False, "rules": None}


def test_one_deployment_per_project(reconcile_module) -> None:
    deps = deploy.build_deployments(
        projects={"a": project("a"), "b": project("b")},
        ids={("a", "primary"): (1, 1), ("b", "primary"): (2, 2)},
    )
    assert set(reconciles(deps)) == {"reconcile-p1-a", "reconcile-p2-b"}


def test_missing_flow_module_skips_deployments(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deploy, "RECONCILE_MODULE", "heliopuddle.server.services.no_reconcile")
    deps = deploy.build_deployments(projects={"demo": project()}, ids=IDS)
    assert not reconciles(deps)


def test_missing_or_ambiguous_project_ids_fail_before_build(reconcile_module) -> None:
    with pytest.raises(ValueError, match="no unique DB id"):
        deploy.reconcile_deployment_specs({"demo": project()}, {})
    with pytest.raises(ValueError, match="no unique DB id"):
        deploy.reconcile_deployment_specs(
            {"demo": project()}, {("demo", "a"): (1, 1), ("demo", "b"): (2, 2)}
        )


def test_colliding_names_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deploy, "reconcile_deployment_name", lambda pid, name: "same")
    with pytest.raises(ValueError, match="collide"):
        deploy.reconcile_deployment_specs(
            {"a": project("a"), "b": project("b")},
            {("a", "primary"): (1, 1), ("b", "primary"): (2, 2)},
        )


def test_stale_deployments_and_removed_schedules_are_deleted(reconcile_module) -> None:
    deps = deploy.build_deployments(projects={"demo": project()}, ids=IDS)
    stale = SimpleNamespace(id=uuid4(), name="reconcile-p7-gone", schedules=[])
    old_schedule = SimpleNamespace(id=uuid4(), slug="reconcile")
    current = SimpleNamespace(id=uuid4(), name="reconcile-p7-demo", schedules=[old_schedule])
    unrelated = SimpleNamespace(id=uuid4(), name="other", schedules=[])
    removed: list[Any] = []
    schedules: list[Any] = []
    client = SimpleNamespace(
        read_deployments=lambda **kw: [stale, current, unrelated],
        delete_deployment=removed.append,
        delete_deployment_schedule=lambda dep, id: schedules.append((dep, id)),
    )
    result = deploy.prepare_reconcile_services(deps, client)  # type: ignore[arg-type]
    assert removed == [stale.id]
    assert schedules == [(current.id, old_schedule.id)]
    assert result == ["reconcile/reconcile-p7-gone"]


def test_scheduled_deployment_keeps_its_schedule(reconcile_module) -> None:
    cfg = project(schedule="0 3 * * *")
    deps = deploy.build_deployments(projects={"demo": cfg}, ids=IDS)
    kept = SimpleNamespace(id=uuid4(), slug="reconcile")
    current = SimpleNamespace(id=uuid4(), name="reconcile-p7-demo", schedules=[kept])
    schedules: list[Any] = []
    client = SimpleNamespace(
        read_deployments=lambda **kw: [current],
        delete_deployment=lambda id: None,
        delete_deployment_schedule=lambda dep, id: schedules.append(id),
    )
    assert deploy.prepare_reconcile_services(deps, client) == []  # type: ignore[arg-type]
    assert schedules == []

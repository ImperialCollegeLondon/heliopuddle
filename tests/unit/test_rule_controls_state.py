"""Rule controls: state, disable/enable, triggers and runs."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from prefect.client.schemas.objects import StateType
from prefect.states import Completed, Pending, Running, Scheduled

from heliopuddle.server import models
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import RuleState
from heliopuddle.server.services import runs
from tests.unit._rule_controls_helpers import (
    SPEC,
    FakePrefect,
    api,
    configured,
    prefect,
)

_FIXTURES = (
    api,
    configured,
    prefect,
)  # referenced so ruff sees the imports used


@pytest.mark.req("API-14", "SVC-25")
def test_rules_include_defaults_runtime_state_and_watermarks(
    api: TestClient,
    configured: AppContext,
    prefect: FakePrefect,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PREFECT_API_URL", "http://prefect/api")
    stamp = datetime(2026, 3, 1, tzinfo=UTC)
    with configured.write_session() as session:
        project = models.get_project(session, "demo")
        assert project is not None
        session.add(
            RuleState(
                project_id=project.id,
                rule_name="archive",
                enabled=False,
                watermark_at=stamp,
                watermark_id=7,
                last_success_at=stamp,
            )
        )
    response = api.get("/projects/demo/rules")
    assert response.status_code == 200
    archive, yaml_off = response.json()
    assert archive["options"]["batch"] == {
        "max_files": 100,
        "max_bytes": 1_000_000_000,
        "max_duration": "30m",
    }
    assert archive["enabled"] is True and archive["effective_enabled"] is False
    assert archive["deployment"] == "run_rule/rule-p1-demo-archive"
    assert archive["deployment_id"] == str(prefect.deployment.id)
    assert (
        archive["deployment_url"]
        == f"http://prefect/deployments/deployment/{prefect.deployment.id}"
    )
    assert datetime.fromisoformat(archive["watermark_at"]) == stamp
    assert archive["watermark_id"] == 7
    assert yaml_off["effective_enabled"] is False
    assert api.get("/projects/demo/rules/archive").json() == archive
    assert api.get("/projects/demo/rules/missing").status_code == 404
    assert api.get("/projects/missing/rules").status_code == 404
    assert (
        api.patch("/projects/demo/rules/archive", json={"source": "secondary"}).status_code == 405
    )


@pytest.mark.req("SVC-25", "API-14")
def test_disable_pauses_cancels_only_scheduled_and_enable_resumes(
    api: TestClient, configured: AppContext, prefect: FakePrefect
) -> None:
    future = prefect.add_run(Scheduled(scheduled_time=datetime.now(UTC) + timedelta(days=1)))
    active = prefect.add_run(Running())
    pending = prefect.add_run(Pending())
    assert api.post("/projects/demo/rules/archive/disable").json()["effective_enabled"] is False
    assert prefect.deployment.paused
    assert not prefect.schedules[0].active
    assert prefect.cancelled == [future.id]
    assert active.state.type == StateType.RUNNING and pending.state.type == StateType.PENDING
    assert configured.projects["demo"].rule("archive").enabled is True
    assert api.post("/projects/demo/rules/archive/trigger").status_code == 409
    assert (
        api.post("/projects/demo/runs", json={"kind": "rule", "rule": "archive"}).status_code == 409
    )
    assert api.post("/projects/demo/rules/archive/enable").json()["effective_enabled"] is True
    assert not prefect.deployment.paused and prefect.schedules[0].active
    assert api.post("/projects/demo/rules/yaml-off/enable").json()["effective_enabled"] is False
    assert prefect.deployment.paused and not prefect.schedules[0].active


@pytest.mark.req("SVC-25", "API-14")
def test_prefect_failure_is_reported_but_disable_remains_persisted(
    api: TestClient, prefect: FakePrefect
) -> None:
    prefect.error = ConnectionError("Prefect unavailable")
    response = api.post("/projects/demo/rules/archive/disable")
    assert response.status_code == 503
    assert "Prefect" in response.json()["detail"]
    assert api.get("/projects/demo/rules/archive").json()["effective_enabled"] is False
    assert api.post("/projects/demo/rules/archive/enable").status_code == 503
    assert api.post("/projects/demo/rules/archive/trigger").status_code == 503


@pytest.mark.req("SVC-24", "API-14")
def test_trigger_during_a_running_run_queues_exactly_one_follow_up(
    api: TestClient, prefect: FakePrefect
) -> None:
    running = prefect.add_run(Running())
    first = api.post("/projects/demo/rules/archive/trigger").json()
    assert first["started"] is True and first["flow_run_id"] != str(running.id)
    assert prefect.submissions[-1]["idempotency_key"].endswith(f"-after-{running.id}")
    # The follow-up is queued and has not selected its work: later triggers are no-ops.
    again = api.post("/projects/demo/rules/archive/trigger").json()
    assert again == {"started": False, "flow_run_id": first["flow_run_id"]}
    assert len(prefect.submissions) == 1


@pytest.mark.req("SVC-24", "API-14")
@pytest.mark.parametrize("state", [Scheduled(), Pending()])
def test_trigger_existing_work_is_successful_noop(
    api: TestClient, prefect: FakePrefect, state: Any
) -> None:
    prefect.add_run(state)
    response = api.post("/projects/demo/rules/archive/trigger")
    assert response.status_code == 200
    assert response.json()["started"] is False
    assert prefect.submissions == []


@pytest.mark.req("SVC-24")
def test_racing_triggers_create_one_run_and_terminal_history_allows_next_generation(
    configured: AppContext, prefect: FakePrefect
) -> None:
    from heliopuddle.server.services import rule_control

    def trigger(_: int) -> Any:
        return rule_control.trigger_rule(configured, "demo", "archive")

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(trigger, range(4)))
    assert sum(result.started for result in results) == 1
    assert len(prefect.runs) == 1
    first = prefect.runs[0]
    assert first.parameters == {
        "project": "demo",
        "rule": "archive",
        "mode": "incremental",
        "dry_run": False,
    }
    first.state = Completed()
    first.state_type = first.state.type
    result = rule_control.trigger_rule(configured, "demo", "archive")
    assert result.started and result.flow_run_id != str(first.id)
    assert len(prefect.runs) == 2
    assert prefect.runs[0].idempotency_key != first.idempotency_key
    assert not rule_control.trigger_rule(configured, "demo", "archive").started


@pytest.mark.req("API-04")
def test_configured_and_unsaved_runs_submit_exact_parameters(
    api: TestClient, monkeypatch: pytest.MonkeyPatch, configured: AppContext
) -> None:
    submitted: list[tuple[str, dict[str, Any]]] = []

    def submit(name: str, **kwargs: Any) -> Any:
        submitted.append((name, kwargs))
        return SimpleNamespace(id=uuid4())

    monkeypatch.setattr(runs, "run_deployment", submit)
    response = api.post(
        "/projects/demo/runs",
        json={"kind": "rule", "rule": "archive", "mode": "full", "dry_run": True},
    )
    assert response.status_code == 202
    assert submitted[-1][0] == "run_rule/rule-p1-demo-archive"
    assert submitted[-1][1]["parameters"] == {
        "project": "demo",
        "rule": "archive",
        "mode": "full",
        "dry_run": True,
    }
    assert submitted[-1][1]["timeout"] == 0
    response = api.post("/projects/demo/runs", json={"kind": "rule_once", "spec": SPEC})
    assert response.status_code == 202
    assert response.json()["deployment"] == "run_rule_once/rule-once"
    parameters = submitted[-1][1]["parameters"]
    assert parameters["project"] == "demo"
    assert parameters["spec"]["action"] == "copy"
    assert parameters["spec"]["options"]["batch"]["max_files"] == 100
    assert len(configured.projects["demo"].rules) == 2
    with configured.sessionmaker() as session:
        assert session.get(RuleState, (1, "archive")) is None
        assert session.get(RuleState, (1, "rule-once")) is None


@pytest.mark.req("API-04")
@pytest.mark.parametrize(
    "body",
    [
        {"kind": "rule", "rule": "archive", "source": "secondary"},
        {"kind": "rule", "rule": "archive", "options": {"overwrite": True}},
        {"kind": "rule", "rule": "archive", "mode": "partial"},
        {"kind": "rule_once", "spec": {**SPEC, "source": "missing"}},
        {"kind": "rule_once", "spec": {**SPEC, "destination": "primary"}},
        {"kind": "rule_once", "spec": {**SPEC, "action": "delete"}},
        {"kind": "rule_once", "spec": {**SPEC, "name": "not-unsaved"}},
        {"kind": "reconcile", "mode": "full"},
    ],
)
def test_run_rejects_overrides_and_invalid_one_off_specs(
    api: TestClient, body: dict[str, Any]
) -> None:
    assert api.post("/projects/demo/runs", json=body).status_code == 422


@pytest.mark.req("API-04")
def test_rule_run_missing_disabled_and_prefect_unavailable(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (
        api.post("/projects/demo/runs", json={"kind": "rule", "rule": "missing"}).status_code == 404
    )
    assert (
        api.post("/projects/demo/runs", json={"kind": "rule", "rule": "yaml-off"}).status_code
        == 409
    )

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise ConnectionError("connection refused")

    monkeypatch.setattr(runs, "run_deployment", unavailable)
    for body in ({"kind": "rule", "rule": "archive"}, {"kind": "rule_once", "spec": SPEC}):
        response = api.post("/projects/demo/runs", json=body)
        assert response.status_code == 503
        assert "connection refused" in response.json()["detail"]

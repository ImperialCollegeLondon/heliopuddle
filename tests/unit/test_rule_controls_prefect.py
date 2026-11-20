"""Rule controls: Prefect failures and pagination."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from prefect.client.schemas.objects import StateType
from prefect.settings import PREFECT_API_URL, temporary_settings
from prefect.states import Completed, Pending, Running, Scheduled

from heliopuddle.server import models
from heliopuddle.server.context import AppContext
from heliopuddle.server.services import rule_prefect, runs
from tests.unit._rule_controls_helpers import (  # noqa: F401
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


@pytest.mark.req("SVC-25", "API-14")
def test_schedule_cancellation_rejection_surfaces_failure(
    api: TestClient,
    prefect: FakePrefect,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefect.add_run(Scheduled())
    monkeypatch.setattr(
        prefect,
        "set_flow_run_state",
        lambda *args, **kwargs: SimpleNamespace(status="REJECT", state=Scheduled()),
    )
    response = api.post("/projects/demo/rules/archive/disable")
    assert response.status_code == 503
    assert "refused cancellation" in response.json()["detail"]
    assert api.get("/projects/demo/rules/archive").json()["effective_enabled"] is False


@pytest.mark.req("SVC-25")
def test_disable_leaves_a_run_that_started_after_the_schedule_probe(
    configured: AppContext,
    prefect: FakePrefect,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    candidate = prefect.add_run(Scheduled())

    def became_running(run_id: Any) -> Any:
        assert run_id == candidate.id
        candidate.state = Running()
        candidate.state_type = StateType.RUNNING
        return candidate

    monkeypatch.setattr(prefect, "read_flow_run", became_running)
    rule_prefect._cancel_scheduled(prefect, prefect.deployment.id)
    assert prefect.cancelled == []
    assert candidate.state_type == StateType.RUNNING


@pytest.mark.req("API-14")
def test_reading_rules_without_prefect_does_not_start_an_implicit_server(
    configured: AppContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from heliopuddle.server.services import rule_control

    calls: list[bool] = []

    def no_server(**kwargs: Any) -> Any:
        calls.append(True)
        raise AssertionError("A config read must not start Prefect")

    monkeypatch.delenv("PREFECT_API_URL", raising=False)
    monkeypatch.setattr(rule_control, "get_client", no_server)
    with temporary_settings({PREFECT_API_URL: None}):
        rules = rule_control.list_rules(configured, "demo")
    assert len(rules) == 2 and rules[0].deployment_id is None
    assert rules[0].deployment_url is None and not calls


@pytest.mark.req("SVC-24")
def test_trigger_generation_walks_past_one_page_of_terminal_history(
    configured: AppContext,
    prefect: FakePrefect,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from heliopuddle.server.services import rule_control

    monkeypatch.setattr(rule_prefect, "_HISTORY_PAGE", 2)
    prefix = f"hp-trigger-{prefect.deployment.id}-g"
    for generation in (7, 5, 6, 2, 1):
        prefect.add_run(Completed(), idempotency_key=f"{prefix}{generation}")
    result = rule_control.trigger_rule(configured, "demo", "archive")
    assert result.started
    assert prefect.runs[0].idempotency_key == f"{prefix}8"
    assert len(prefect.submissions) == 1


@pytest.mark.req("SVC-25")
def test_disable_cancels_every_page_of_scheduled_runs(
    api: TestClient,
    prefect: FakePrefect,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    monkeypatch.setattr(rule_prefect, "_HISTORY_PAGE", 2)
    future = [prefect.add_run(Scheduled()) for _ in range(5)]
    running = prefect.add_run(Running())
    response = api.post("/projects/demo/rules/archive/disable")
    assert response.status_code == 200
    assert set(prefect.cancelled) == {run.id for run in future}
    assert running.state_type == StateType.RUNNING


@pytest.mark.req("SVC-24", "SVC-25")
@pytest.mark.parametrize("operation", ["trigger", "disable"])
def test_prefect_outage_probe_does_not_hold_up_unrelated_file_changes(
    configured: AppContext,
    prefect: FakePrefect,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    from heliopuddle.server.services import rule_control

    with configured.write_session() as session:
        file = models.register_file(
            session,
            project="demo",
            storage_location="primary",
            path="data/file.dat",
            hash="abc",
            size=1,
        )
    done = threading.Event()
    errors: list[Exception] = []
    completed_during_probe: list[bool] = []

    def write_file() -> None:
        try:
            with configured.write_session() as session:
                models.update_metadata(session, file.file_id, {"during": "Prefect outage"})
        except Exception as error:
            errors.append(error)
        finally:
            done.set()

    worker = threading.Thread(target=write_file)

    def unavailable(name: str) -> Any:
        worker.start()
        completed_during_probe.append(done.wait(0.5))
        raise ConnectionError("Prefect unavailable")

    monkeypatch.setattr(prefect, "read_deployment_by_name", unavailable)
    try:
        with pytest.raises(runs.RunSubmitError, match="Prefect unavailable"):
            if operation == "trigger":
                rule_control.trigger_rule(configured, "demo", "archive")
            else:
                rule_control.set_rule_enabled(configured, "demo", "archive", False)
    finally:
        if worker.ident is not None:
            worker.join(timeout=5)
    assert not worker.is_alive() and not errors
    assert completed_during_probe == [True]


@pytest.mark.req("API-04", "SVC-24")
@pytest.mark.parametrize("state", [Scheduled(), Pending(), Running()])
def test_manual_run_is_refused_while_the_rule_has_an_unfinished_run(
    api: TestClient, prefect: FakePrefect, monkeypatch: pytest.MonkeyPatch, state: Any
) -> None:
    submitted: list[str] = []
    monkeypatch.setattr(runs, "run_deployment", lambda name, **_: submitted.append(name))
    active = prefect.add_run(state)
    response = api.post("/projects/demo/runs", json={"kind": "rule", "rule": "archive"})
    assert response.status_code == 409
    assert str(active.id) in response.json()["detail"]
    assert submitted == []


@pytest.mark.req("SVC-24")
def test_rule_control_requests_ride_out_a_transient_prefect_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx
    from prefect.client import base as prefect_base
    from prefect.client.orchestration import get_client

    answers = iter([503, 503, 200])
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(next(answers), json={"status": "ok"})

    monkeypatch.setattr(prefect_base.time, "sleep", lambda _s: None)
    with (
        temporary_settings({PREFECT_API_URL: "http://prefect.test/api"}),
        rule_prefect._prefect_requests(),
        get_client(
            sync_client=True, httpx_settings={"transport": httpx.MockTransport(handler)}
        ) as client,
    ):
        response = client.request("GET", "/health")
    assert response.status_code == 200
    assert len(seen) == 3

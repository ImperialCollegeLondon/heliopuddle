"""Scheduler-created future runs are not "unfinished" for manual runs and triggers."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from prefect.settings import PREFECT_API_URL, temporary_settings
from prefect.states import Scheduled

from heliopuddle.config.models import RuleConfig
from heliopuddle.server.api import create_api
from heliopuddle.server.context import AppContext
from heliopuddle.server.services import rule_control, rule_prefect, runs
from tests.unit._rule_controls_helpers import SPEC, FakePrefect


@pytest.fixture
def configured(ctx: AppContext) -> AppContext:
    ctx.projects["demo"] = ctx.projects["demo"].model_copy(
        update={"rules": [RuleConfig(name="archive", schedule="0 2 * * *", **SPEC)]}
    )
    return ctx


@pytest.fixture
def prefect(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakePrefect]:
    client = FakePrefect()
    monkeypatch.setattr(rule_control, "get_client", lambda **kwargs: client)
    with temporary_settings({PREFECT_API_URL: "http://prefect.test/api"}):
        yield client


@pytest.fixture
def submitted(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    calls: list[Any] = []

    def submit(name: str, **kwargs: Any) -> Any:
        calls.append(name)
        return SimpleNamespace(id=uuid4())

    monkeypatch.setattr(runs, "run_deployment", submit)
    return calls


@pytest.fixture
def api(configured: AppContext, prefect: FakePrefect) -> TestClient:
    return TestClient(create_api(configured))


def scheduled(prefect: FakePrefect, *, auto: bool, minutes: float, name: str = "Scheduled") -> Any:
    start = datetime.now(UTC) + timedelta(minutes=minutes)
    state = Scheduled(scheduled_time=start)
    state.name = name
    run = prefect.add_run(state)
    run.auto_scheduled = auto
    run.expected_start_time = start
    return run


BODIES = [
    ("rule", {"kind": "rule", "rule": "archive"}),
    ("reconcile", {"kind": "reconcile"}),
]


@pytest.mark.req("API-04", "SVC-27")
@pytest.mark.parametrize("body", [b for _, b in BODIES], ids=[n for n, _ in BODIES])
def test_future_auto_scheduled_run_does_not_block_a_manual_run(
    api: TestClient, prefect: FakePrefect, submitted: list[Any], body: dict[str, Any]
) -> None:
    scheduled(prefect, auto=True, minutes=60)
    assert api.post("/projects/demo/runs", json=body).status_code == 202
    assert len(submitted) == 1


@pytest.mark.req("API-04", "SVC-27")
@pytest.mark.parametrize("body", [b for _, b in BODIES], ids=[n for n, _ in BODIES])
@pytest.mark.parametrize(
    ("auto", "minutes", "name"),
    [
        (True, -1, "Scheduled"),  # due or overdue
        (True, 60, "AwaitingConcurrencySlot"),
        (False, 60, "Scheduled"),  # hp-created continuation
        (False, 0.1, "Scheduled"),  # hp-created, a few seconds ahead
    ],
)
def test_other_scheduled_runs_still_block_with_409(
    api: TestClient,
    prefect: FakePrefect,
    submitted: list[Any],
    body: dict[str, Any],
    auto: bool,
    minutes: float,
    name: str,
) -> None:
    run = scheduled(prefect, auto=auto, minutes=minutes, name=name)
    response = api.post("/projects/demo/runs", json=body)
    assert response.status_code == 409
    assert str(run.id) in response.json()["detail"]
    assert not submitted


@pytest.mark.req("API-04", "SVC-27")
def test_a_real_run_behind_many_future_cron_runs_still_blocks(
    api: TestClient, prefect: FakePrefect, submitted: list[Any]
) -> None:
    real = scheduled(prefect, auto=False, minutes=30)
    for i in range(rule_prefect._HISTORY_PAGE + 5):
        scheduled(prefect, auto=True, minutes=60 + i)
    prefect.runs.remove(real)
    prefect.runs.append(real)  # last: only found by paging
    response = api.post("/projects/demo/runs", json={"kind": "reconcile"})
    assert response.status_code == 409
    assert str(real.id) in response.json()["detail"]


@pytest.mark.req("SVC-24")
def test_future_auto_scheduled_run_does_not_make_a_trigger_a_no_op(
    configured: AppContext, prefect: FakePrefect
) -> None:
    scheduled(prefect, auto=True, minutes=60)
    assert rule_control.trigger_rule(configured, "demo", "archive").started
    assert len(prefect.submissions) == 1


@pytest.mark.req("SVC-24")
@pytest.mark.parametrize(
    ("auto", "minutes", "name"),
    [(True, -1, "Scheduled"), (False, 60, "Scheduled"), (True, 60, "AwaitingConcurrencySlot")],
)
def test_other_scheduled_runs_still_make_a_trigger_a_no_op(
    configured: AppContext, prefect: FakePrefect, auto: bool, minutes: float, name: str
) -> None:
    run = scheduled(prefect, auto=auto, minutes=minutes, name=name)
    result = rule_control.trigger_rule(configured, "demo", "archive")
    assert not result.started
    assert result.flow_run_id == str(run.id)
    assert not prefect.submissions

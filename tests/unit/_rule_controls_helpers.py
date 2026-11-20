"""Shared helpers and fixtures for the split rule_controls tests."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from prefect.client.schemas.objects import StateType
from prefect.settings import PREFECT_API_URL, temporary_settings
from prefect.states import Scheduled

from heliopuddle.config.models import RuleConfig
from heliopuddle.server.api import create_api
from heliopuddle.server.context import AppContext

SPEC = {
    "action": "copy",
    "source": "primary",
    "destination": "secondary",
    "match": {"paths": ["data/**"], "exclude": ["data/private/**"]},
}


class FakePrefect:
    """Keep deployment runs across calls, like a restarted API would see them."""

    def __init__(self) -> None:
        self.deployment = SimpleNamespace(id=uuid4(), paused=False)
        self.schedules = [SimpleNamespace(id=uuid4(), active=True)]
        self.runs: list[Any] = []
        self.submissions: list[dict[str, Any]] = []
        self.cancelled: list[Any] = []
        self.error: Exception | None = None

    def __enter__(self) -> FakePrefect:
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def read_deployment_by_name(self, name: str) -> Any:
        if self.error:
            raise self.error
        return self.deployment

    def read_flow_runs(self, **kwargs: Any) -> list[Any]:
        state_filter = kwargs.get("flow_run_filter")
        types = state_filter.state.type.any_ if state_filter and state_filter.state else None
        selected = [run for run in self.runs if types is None or run.state.type in types]
        offset = kwargs.get("offset", 0)
        limit = kwargs.get("limit", 200)
        return selected[offset : offset + limit]

    def create_flow_run_from_deployment(self, deployment_id: Any, **kwargs: Any) -> Any:
        self.submissions.append(kwargs)
        for run in self.runs:
            if run.idempotency_key == kwargs.get("idempotency_key"):
                return run
        run = self.add_run(Scheduled(), idempotency_key=kwargs.get("idempotency_key"))
        run.parameters = kwargs["parameters"]
        return run

    def add_run(self, state: Any, idempotency_key: str | None = None) -> Any:
        run = SimpleNamespace(
            id=uuid4(),
            state=state,
            state_type=state.type,
            idempotency_key=idempotency_key,
            created=datetime.now(UTC),
        )
        self.runs.insert(0, run)
        return run

    def read_flow_run(self, run_id: Any) -> Any:
        return next(run for run in self.runs if run.id == run_id)

    def set_deployment_paused_state(self, deployment_id: Any, paused: bool) -> None:
        self.deployment.paused = paused

    def read_deployment_schedules(self, deployment_id: Any) -> list[Any]:
        return self.schedules

    def update_deployment_schedule(
        self, deployment_id: Any, schedule_id: Any, *, active: bool
    ) -> None:
        next(s for s in self.schedules if s.id == schedule_id).active = active

    def set_flow_run_state(self, run_id: Any, state: Any, *, force: bool = False) -> Any:
        assert not force
        run = next(r for r in self.runs if r.id == run_id)
        assert run.state.type == StateType.SCHEDULED
        run.state = state
        run.state_type = state.type
        self.cancelled.append(run_id)
        return SimpleNamespace(status="ACCEPT", state=state)


@pytest.fixture
def configured(ctx: AppContext) -> AppContext:
    ctx.projects["demo"] = ctx.projects["demo"].model_copy(
        update={
            "rules": [
                RuleConfig(name="archive", schedule="0 2 * * *", **SPEC),
                RuleConfig(name="yaml-off", enabled=False, **SPEC),
            ]
        }
    )
    return ctx


@pytest.fixture
def prefect(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakePrefect]:
    from heliopuddle.server.services import rule_control

    client = FakePrefect()
    monkeypatch.setattr(rule_control, "get_client", lambda **kwargs: client)
    with temporary_settings({PREFECT_API_URL: "http://prefect.test/api"}):
        yield client


@pytest.fixture
def api(configured: AppContext, prefect: FakePrefect) -> TestClient:
    return TestClient(create_api(configured))

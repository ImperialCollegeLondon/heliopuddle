"""Reconcile runs through the API and the client (API-04, CLI-10), without Prefect."""

from __future__ import annotations

import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from prefect.settings import PREFECT_API_URL, temporary_settings
from prefect.states import Completed, Pending, Running, Scheduled

from heliopuddle.client import HelioPuddleClient, HelioPuddleError
from heliopuddle.config.models import RuleConfig
from heliopuddle.server.api import create_api
from heliopuddle.server.api.deps import get_principal
from heliopuddle.server.auth.principal import Principal
from heliopuddle.server.context import AppContext
from heliopuddle.server.services import runs
from tests.unit._rule_controls_helpers import SPEC, FakePrefect


@pytest.fixture
def configured(ctx: AppContext) -> AppContext:
    ctx.projects["demo"] = ctx.projects["demo"].model_copy(
        update={"rules": [RuleConfig(name="archive", **SPEC), RuleConfig(name="other", **SPEC)]}
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
def submitted(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    def submit(name: str, **kwargs: Any) -> Any:
        calls.append((name, kwargs))
        return SimpleNamespace(id=uuid4())

    monkeypatch.setattr(runs, "run_deployment", submit)
    return calls


@pytest.fixture
def api(configured: AppContext, prefect: FakePrefect) -> TestClient:
    return TestClient(create_api(configured))


@pytest.mark.req("API-04")
@pytest.mark.parametrize(
    ("body", "parameters"),
    [
        ({"kind": "reconcile"}, {"project": "demo", "verify": False, "rules": None}),
        (
            {"kind": "reconcile", "verify": True, "rules": ["archive"]},
            {"project": "demo", "verify": True, "rules": ["archive"]},
        ),
        ({"kind": "reconcile", "rules": []}, {"project": "demo", "verify": False, "rules": []}),
    ],
)
def test_reconcile_run_is_accepted_with_exact_parameters(
    api: TestClient, submitted: list[Any], body: dict[str, Any], parameters: dict[str, Any]
) -> None:
    response = api.post("/projects/demo/runs", json=body)
    assert response.status_code == 202
    assert response.json()["deployment"] == "reconcile/reconcile-p1-demo"
    assert submitted[-1][0] == "reconcile/reconcile-p1-demo"
    assert submitted[-1][1]["parameters"] == parameters
    assert submitted[-1][1]["timeout"] == 0


@pytest.mark.req("API-04", "AUTH-07", "AUTH-08")
@pytest.mark.parametrize(
    ("roles", "status"),
    [
        (["hp-admin"], 202),
        (["hp-operator"], 202),
        (["hp-operator:demo"], 202),
        (["hp-reader"], 403),
        (["hp-writer:demo"], 403),
        (["hp-deleter"], 403),
        (["hp-operator:other"], 404),
        ([], 404),
    ],
)
def test_reconcile_requires_an_operator_on_its_project(
    configured: AppContext,
    prefect: FakePrefect,
    submitted: list[Any],
    roles: list[str],
    status: int,
) -> None:
    app = create_api(configured)
    app.dependency_overrides[get_principal] = lambda: Principal("tester", tuple(roles), "local")
    response = TestClient(app).post("/projects/demo/runs", json={"kind": "reconcile"})
    assert response.status_code == status, response.text
    assert len(submitted) == (1 if status == 202 else 0)


@pytest.mark.req("API-04")
def test_unknown_rule_names_are_422_and_submit_nothing(
    api: TestClient, submitted: list[Any]
) -> None:
    response = api.post(
        "/projects/demo/runs", json={"kind": "reconcile", "rules": ["archive", "nope"]}
    )
    assert response.status_code == 422
    assert "nope" in response.json()["detail"]
    assert not submitted


@pytest.mark.req("API-04")
@pytest.mark.parametrize("state", [Scheduled, Pending, Running])
def test_unfinished_reconcile_is_409_naming_the_run(
    api: TestClient, prefect: FakePrefect, submitted: list[Any], state: Any
) -> None:
    run = prefect.add_run(state())
    response = api.post("/projects/demo/runs", json={"kind": "reconcile"})
    assert response.status_code == 409
    assert str(run.id) in response.json()["detail"]
    assert not submitted


@pytest.mark.req("API-04")
def test_finished_reconcile_does_not_block(
    api: TestClient, prefect: FakePrefect, submitted: list[Any]
) -> None:
    prefect.add_run(Completed())
    assert api.post("/projects/demo/runs", json={"kind": "reconcile"}).status_code == 202
    assert len(submitted) == 1


@pytest.mark.req("API-04")
def test_reconcile_503_when_prefect_or_deployment_unavailable(
    api: TestClient, prefect: FakePrefect, submitted: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    prefect.error = ConnectionError("connection refused")
    response = api.post("/projects/demo/runs", json={"kind": "reconcile"})
    assert response.status_code == 503
    assert "connection refused" in response.json()["detail"]
    prefect.error = None

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise ConnectionError("submit refused")

    monkeypatch.setattr(runs, "run_deployment", unavailable)
    response = api.post("/projects/demo/runs", json={"kind": "reconcile"})
    assert response.status_code == 503
    assert "submit refused" in response.json()["detail"]


@pytest.mark.req("API-04")
def test_reconcile_unknown_project_is_404(api: TestClient) -> None:
    assert api.post("/projects/none/runs", json={"kind": "reconcile"}).status_code == 404


@pytest.mark.req("CLI-10")
def test_client_reconcile_sends_exact_body_and_parses_run_info() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            202, json={"flow_run_id": "id", "deployment": "reconcile/reconcile-p1-demo"}
        )

    with httpx.Client(base_url="http://api", transport=httpx.MockTransport(respond)) as http:
        hp = HelioPuddleClient("", "demo", http_client=http)
        assert hp.reconcile().flow_run_id == "id"
        assert json.loads(requests[-1].content) == {
            "kind": "reconcile",
            "verify": False,
            "rules": None,
        }
        hp.reconcile(verify=True, rules=["archive"])
        assert json.loads(requests[-1].content)["rules"] == ["archive"]
        assert requests[-1].url.path == "/projects/demo/runs"


@pytest.mark.req("CLI-10")
@pytest.mark.parametrize("status", [409, 422])
def test_client_reconcile_raises_on_conflict_and_invalid(status: int) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"detail": "refused"})

    with httpx.Client(base_url="http://api", transport=httpx.MockTransport(respond)) as http:
        hp = HelioPuddleClient("", "demo", http_client=http)
        with pytest.raises(HelioPuddleError) as error:
            hp.reconcile()
    assert error.value.status_code == status


@pytest.mark.req("CLI-10", "API-04")
def test_client_reconcile_against_the_in_process_api(
    configured: AppContext, prefect: FakePrefect, submitted: list[Any]
) -> None:
    app = TestClient(create_api(configured))
    with HelioPuddleClient("", "demo", http_client=app) as hp:  # type: ignore[arg-type]
        assert hp.reconcile(rules=["other"]).deployment == "reconcile/reconcile-p1-demo"
        assert submitted[-1][1]["parameters"]["rules"] == ["other"]
        prefect.add_run(Running())
        with pytest.raises(HelioPuddleError) as error:
            hp.reconcile()
        assert error.value.status_code == 409
        with pytest.raises(HelioPuddleError) as error:
            hp.reconcile(rules=["missing"])
        assert error.value.status_code == 422


@pytest.mark.req("API-04", "SVC-27")
def test_racing_submissions_start_one_reconcile_and_refuse_the_other(
    configured: AppContext, prefect: FakePrefect, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading
    import time

    from prefect.states import Scheduled

    started: list[str] = []
    barrier = threading.Barrier(2)

    def submit(name: str, **kwargs: Any) -> Any:
        time.sleep(0.2)  # widen the check-to-submit window
        run = prefect.add_run(Scheduled())
        started.append(str(run.id))
        return run

    monkeypatch.setattr(runs, "run_deployment", submit)
    api = TestClient(create_api(configured))
    codes: list[int] = []

    def post() -> None:
        barrier.wait(timeout=5)
        codes.append(api.post("/projects/demo/runs", json={"kind": "reconcile"}).status_code)

    threads = [threading.Thread(target=post) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(codes) == [202, 409]
    assert len(started) == 1

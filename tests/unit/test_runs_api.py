"""Index run endpoint and index times on storage locations (API-10).

Run submission is mocked: ``run_deployment`` is replaced in
:mod:`heliopuddle.server.services.runs`.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from prefect.exceptions import ObjectNotFound

from heliopuddle.server import models
from heliopuddle.server.api import create_api
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import IndexProgress
from heliopuddle.server.services import runs
from heliopuddle.settings import Settings

RUN_ID = uuid.UUID("6f1d2c3b-4a59-4e8d-9c7b-1a2b3c4d5e6f")


@dataclasses.dataclass
class FakeFlowRun:
    id: uuid.UUID


@pytest.fixture
def submitted(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, dict[str, Any]]]:
    """Replace ``run_deployment``; record each call."""
    calls: list[tuple[Any, dict[str, Any]]] = []

    def fake(name: Any, **kwargs: Any) -> FakeFlowRun:
        calls.append((name, kwargs))
        return FakeFlowRun(RUN_ID)

    monkeypatch.setattr(runs, "run_deployment", fake)
    return calls


@pytest.fixture(autouse=True)
def _prefect_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PREFECT_API_URL", "http://prefect.local:4200/api")


@pytest.fixture
def api(ctx: AppContext) -> Iterator[TestClient]:
    with TestClient(create_api(ctx)) as c:
        yield c


def _set_active(ctx: AppContext, name: str, active: bool) -> None:
    with ctx.sessionmaker() as session:
        row = models.get_storage_location(session, "demo", name)
        assert row is not None
        row.active = active
        session.commit()


# --- POST /projects/{p}/runs ----------------------------------------------


@pytest.mark.req("API-10")
def test_index_run_defaults_to_first_storage_location_and_full(
    api: TestClient, submitted: list[tuple[Any, dict[str, Any]]]
) -> None:
    resp = api.post("/projects/demo/runs", json={"kind": "index"})
    assert resp.status_code == 202
    assert resp.json() == {
        "flow_run_id": str(RUN_ID),
        "flow_run_url": f"http://prefect.local:4200/runs/flow-run/{RUN_ID}",
        "deployment": "index_full/index-full-p1-sl1-demo-primary",
    }
    name, kwargs = submitted[0]
    assert name == "index_full/index-full-p1-sl1-demo-primary"
    assert kwargs["timeout"] == 0  # does not wait


@pytest.mark.req("API-10", "SVC-13")
def test_index_run_named_storage_location_incremental(
    api: TestClient, submitted: list[tuple[Any, dict[str, Any]]]
) -> None:
    body = {"kind": "index", "storage_location": "secondary", "mode": "incremental"}
    resp = api.post("/projects/demo/runs", json=body)
    assert resp.status_code == 202
    assert resp.json()["deployment"] == "index_incremental/index-incremental-p1-sl2-demo-secondary"
    assert submitted[0][0] == "index_incremental/index-incremental-p1-sl2-demo-secondary"
    # explicit parameters, never the deployment's defaults
    assert submitted[0][1]["parameters"] == {"project": "demo", "storage_location": "secondary"}


@pytest.mark.req("API-10")
@pytest.mark.parametrize(
    ("url", "body"),
    [
        ("/projects/nope/runs", {"kind": "index"}),
        ("/projects/demo/runs", {"kind": "index", "storage_location": "nope"}),
    ],
)
def test_index_run_unknown_project_or_storage_location_is_404(
    api: TestClient, submitted: list[Any], url: str, body: dict[str, Any]
) -> None:
    resp = api.post(url, json=body)
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]
    assert submitted == []


@pytest.mark.req("API-10")
def test_index_run_inactive_storage_location_is_409(
    api: TestClient, ctx: AppContext, submitted: list[Any]
) -> None:
    _set_active(ctx, "secondary", False)
    resp = api.post("/projects/demo/runs", json={"kind": "index", "storage_location": "secondary"})
    assert resp.status_code == 409
    assert "inactive" in resp.json()["detail"]
    assert submitted == []


@pytest.mark.req("API-10")
@pytest.mark.parametrize(
    "body",
    [
        {"kind": "rule"},
        {"kind": "index", "mode": "partial"},
        {"mode": "full"},
        {"kind": "index", "extra": 1},
    ],
)
def test_index_run_bad_body_is_422(
    api: TestClient, submitted: list[Any], body: dict[str, Any]
) -> None:
    assert api.post("/projects/demo/runs", json=body).status_code == 422
    assert submitted == []


@pytest.mark.req("API-10")
def test_index_run_missing_deployment_is_503(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(name: Any, **kwargs: Any) -> Any:
        raise ObjectNotFound(http_exc=Exception("404"))

    monkeypatch.setattr(runs, "run_deployment", missing)
    resp = api.post("/projects/demo/runs", json={"kind": "index"})
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert "index_full/index-full-p1-sl1-demo-primary" in detail
    assert "not found" in detail


@pytest.mark.req("API-10")
def test_index_run_prefect_unreachable_is_503(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def down(name: Any, **kwargs: Any) -> Any:
        raise ConnectionError("connection refused")

    monkeypatch.setattr(runs, "run_deployment", down)
    resp = api.post("/projects/demo/runs", json={"kind": "index"})
    assert resp.status_code == 503
    assert "connection refused" in resp.json()["detail"]


@pytest.mark.req("API-10")
def test_runs_route_is_post_only(api: TestClient) -> None:
    assert api.get("/projects/demo/runs").status_code == 405


# --- flow run URL ----------------------------------------------------------


@pytest.mark.req("API-10")
@pytest.mark.parametrize(
    ("configured", "env", "expected"),
    [
        ("https://prefect.example/ui/", "http://ignored/api", "https://prefect.example/ui"),
        (None, "http://p:4200/api", "http://p:4200"),
        (None, None, None),
    ],
)
def test_flow_run_url(
    monkeypatch: pytest.MonkeyPatch, configured: str | None, env: str | None, expected: str | None
) -> None:
    if env is None:
        monkeypatch.delenv("PREFECT_API_URL", raising=False)
    else:
        monkeypatch.setenv("PREFECT_API_URL", env)
    url = runs.flow_run_url("abc", Settings(prefect_ui_url=configured))
    assert url == (f"{expected}/runs/flow-run/abc" if expected else None)


@pytest.mark.req("API-10")
def test_submit_index_run_uses_process_settings_by_default(
    monkeypatch: pytest.MonkeyPatch, submitted: list[tuple[Any, dict[str, Any]]]
) -> None:
    monkeypatch.setattr(runs, "get_settings", lambda: Settings(prefect_ui_url="http://ui"))
    info = runs.submit_index_run(
        "demo", "primary", "incremental", project_id=1, storage_location_id=1
    )
    assert info.flow_run_url == f"http://ui/runs/flow-run/{RUN_ID}"
    assert info.deployment == "index_incremental/index-incremental-p1-sl1-demo-primary"
    assert submitted[0][1]["as_subflow"] is False


# --- index times on storage locations --------------------------------------


@pytest.mark.req("API-10")
def test_storage_locations_include_index_times(api: TestClient, ctx: AppContext) -> None:
    full = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    incremental = datetime(2026, 3, 2, 6, 30, tzinfo=UTC)
    with ctx.sessionmaker() as session:
        row = models.get_storage_location(session, "demo", "primary")
        assert row is not None
        session.add(
            IndexProgress(
                storage_location_id=row.id, last_full_at=full, last_incremental_at=incremental
            )
        )
        session.commit()

    listed = {s["name"]: s for s in api.get("/projects/demo/storage-locations").json()}
    assert datetime.fromisoformat(listed["primary"]["last_full_index_at"]) == full
    assert datetime.fromisoformat(listed["primary"]["last_incremental_index_at"]) == incremental
    assert listed["secondary"]["last_full_index_at"] is None
    assert listed["secondary"]["last_incremental_index_at"] is None

    detail = api.get("/projects/demo/storage-locations/primary").json()
    assert datetime.fromisoformat(detail["last_full_index_at"]) == full
    assert datetime.fromisoformat(detail["last_incremental_index_at"]) == incremental
    never = api.get("/projects/demo/storage-locations/secondary").json()
    assert never["last_full_index_at"] is None
    assert never["last_incremental_index_at"] is None


@pytest.mark.req("API-10")
def test_get_index_progress_with_no_ids(ctx: AppContext) -> None:
    with ctx.sessionmaker() as session:
        assert models.get_index_progress(session, []) == {}

"""Index run submission against a real (test harness) Prefect server (API-10)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from prefect.client.orchestration import get_client

from heliopuddle.server.api import create_api
from heliopuddle.server.context import AppContext
from heliopuddle.server.services import runs

pytestmark = pytest.mark.usefixtures("prefect_harness")


@pytest.fixture
def api(ctx: AppContext) -> Iterator[TestClient]:
    with TestClient(create_api(ctx)) as c:
        yield c


@pytest.mark.req("API-10")
def test_submits_a_scheduled_run_of_the_index_deployment(api: TestClient) -> None:
    with get_client(sync_client=True) as client:
        flow_id = client.create_flow_from_name("index_incremental")
        deployment_id = client.create_deployment(
            flow_id=flow_id, name="index-incremental-p1-sl2-demo-secondary"
        )

    body = {"kind": "index", "storage_location": "secondary", "mode": "incremental"}
    resp = api.post("/projects/demo/runs", json=body)
    assert resp.status_code == 202, resp.text
    info = resp.json()
    assert info["deployment"] == "index_incremental/index-incremental-p1-sl2-demo-secondary"

    with get_client(sync_client=True) as client:
        flow_run = client.read_flow_run(info["flow_run_id"])
    assert flow_run.deployment_id == deployment_id
    assert flow_run.state is not None and flow_run.state.is_scheduled()


@pytest.mark.req("API-10")
def test_missing_deployment_is_a_clear_error(api: TestClient) -> None:
    # Other tests share this Prefect harness and may have registered it.
    from prefect.exceptions import ObjectNotFound

    with get_client(sync_client=True) as client:
        try:
            dep = client.read_deployment_by_name("index_full/index-full-p1-sl1-demo-primary")
        except ObjectNotFound:
            pass
        else:
            client.delete_deployment(dep.id)
    with pytest.raises(runs.RunSubmitError, match="not found"):
        runs.submit_index_run("demo", "primary", "full", project_id=1, storage_location_id=1)
    resp = api.post("/projects/demo/runs", json={"kind": "index"})
    assert resp.status_code == 503
    assert "index_full/index-full-p1-sl1-demo-primary" in resp.json()["detail"]

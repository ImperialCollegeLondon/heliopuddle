"""DEP-03: the Docker image with an Azure Blob (Azurite) storage location.

Blobs already in the container are adopted on start; a client add from the
host writes straight to Azurite and ends ``available``; a blob deleted
directly is picked up by a full index. Needs the index flows and the runs
endpoint (with a fallback to submitting the deployment run through Prefect).
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from heliopuddle.client import HelioPuddleClient
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services.index_names import index_deployment_full_name

from .conftest import AZURE_EXISTING, AZURE_PROJECT, AzureHp

pytestmark = pytest.mark.req("DEP-03")

TIMEOUT_S = 180.0
FINAL = {"COMPLETED", "FAILED", "CRASHED", "CANCELLED"}


@pytest.fixture
def az_client(hp_azure: AzureHp) -> Iterator[HelioPuddleClient]:
    """A client on the host, talking to the Azure-backed container's API."""
    with HelioPuddleClient(hp_azure.hp.api_url, project=AZURE_PROJECT, user="e2e") as c:
        yield c


def _files(hp_azure: AzureHp, **params: Any) -> dict[str, dict[str, Any]]:
    r = httpx.get(
        f"{hp_azure.hp.api_url}/projects/{AZURE_PROJECT}/files",
        params={"limit": 500, **params},
        timeout=30,
    )
    assert r.status_code == 200, r.text
    return {f["path"]: f for f in r.json()["items"]}


def _wait_adopted(hp_azure: AzureHp) -> dict[str, dict[str, Any]]:
    deadline = time.monotonic() + TIMEOUT_S
    files: dict[str, dict[str, Any]] = {}
    while time.monotonic() < deadline:
        files = _files(hp_azure)
        if set(AZURE_EXISTING) <= set(files):
            return files
        time.sleep(2)
    pytest.fail(f"existing blobs not adopted after {TIMEOUT_S:.0f}s; files: {sorted(files)}")


def _submit_full_index(hp_azure: AzureHp) -> str:
    """Submit a full index: the runs endpoint, else the deployment run through Prefect."""
    r = httpx.post(
        f"{hp_azure.hp.api_url}/projects/{AZURE_PROJECT}/runs",
        json={"kind": "index", "storage_location": "blob", "mode": "full"},
        timeout=30,
    )
    if r.status_code == 202:
        return r.json()["flow_run_id"]
    assert r.status_code in {404, 405}, r.text  # endpoint not there yet
    prefect = httpx.Client(base_url=hp_azure.prefect_url, timeout=30)
    name = index_deployment_full_name("full", 1, 1, AZURE_PROJECT, "blob")
    dep = prefect.get(f"/deployments/name/{name}")
    assert dep.status_code == 200, dep.text
    run = prefect.post(f"/deployments/{dep.json()['id']}/create_flow_run", json={})
    assert run.status_code == 201, run.text
    return run.json()["id"]


def _wait_run(hp_azure: AzureHp, run_id: str) -> str:
    prefect = httpx.Client(base_url=hp_azure.prefect_url, timeout=30)
    deadline = time.monotonic() + TIMEOUT_S
    state = "UNKNOWN"
    while time.monotonic() < deadline:
        state = prefect.get(f"/flow_runs/{run_id}").json()["state"]["type"]
        if state in FINAL:
            break
        time.sleep(1)
    return state


def test_existing_blobs_are_adopted_on_start(hp_azure: AzureHp) -> None:
    files = _wait_adopted(hp_azure)

    for rel, data in AZURE_EXISTING.items():
        record = files[rel]
        [copy] = record["storage_locations"]
        assert copy["storage_location"] == "blob"
        assert copy["state"] == FileState.AVAILABLE
        assert record["hash"] == hashlib.md5(data).hexdigest()
    assert files["obs/2026/obs_v002.csv"]["is_latest"]
    assert not files["obs/2026/obs_v001.csv"]["is_latest"]


def test_client_add_writes_straight_to_azurite(
    hp_azure: AzureHp, az_client: HelioPuddleClient, tmp_path: Path
) -> None:
    content = b"written by the host client\n"
    local = tmp_path / "added.txt"
    local.write_bytes(content)

    record = az_client.add_file(local, "added/from_host.txt")

    assert record.state == FileState.AVAILABLE
    assert hp_azure.blob_fs().cat_file(hp_azure.blob_path("added/from_host.txt")) == content
    assert az_client.get_file(record.id).state == FileState.AVAILABLE


def test_blob_deleted_directly_is_removed_by_a_full_index(hp_azure: AzureHp) -> None:
    files = _wait_adopted(hp_azure)
    victim = files["docs/readme.txt"]
    hp_azure.blob_fs().rm(hp_azure.blob_path("docs/readme.txt"))

    run_id = _submit_full_index(hp_azure)

    assert _wait_run(hp_azure, run_id) == "COMPLETED"
    r = httpx.get(f"{hp_azure.hp.api_url}/files/{victim['id']}", timeout=30)
    assert r.status_code == 200, r.text
    record = r.json()
    assert record["deleted_at"] is not None
    assert [c["state"] for c in record["storage_locations"]] == [FileState.DELETED]
    assert "docs/readme.txt" not in _files(hp_azure)

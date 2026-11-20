"""Shared helpers for the rules server acceptance tests."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from heliopuddle.client import HelioPuddleClient
from heliopuddle.server.schemas import FileRead, FileState

from ..conftest import Deployment
from ..test_postgres_server import _prefect_call

PROJECT = "rules-acceptance"


FINAL = {"COMPLETED", "FAILED", "CRASHED", "CANCELLED"}


TIMEOUT = 240.0


def wait_run(server: Deployment, run_id: str) -> dict[str, Any]:
    """Wait for a specific run, retaining the server log on timeout."""
    deadline = time.monotonic() + TIMEOUT
    with httpx.Client(base_url=server.prefect_url, timeout=30) as prefect:
        while time.monotonic() < deadline:
            response = _prefect_call(prefect, "GET", f"/flow_runs/{run_id}")
            response.raise_for_status()
            run = response.json()
            if run["state"]["type"] in FINAL:
                return run
            time.sleep(0.25)
    pytest.fail(f"run {run_id} did not finish:\n{server.process.log()[-8000:]}")


def deployment(server: Deployment, name: str) -> dict[str, Any]:
    """Read the real Prefect deployment behind a rule or index."""
    with httpx.Client(base_url=server.prefect_url, timeout=30) as prefect:
        response = _prefect_call(prefect, "GET", f"/deployments/name/{name}")
        response.raise_for_status()
        return response.json()


def runs_for(server: Deployment, deployment_id: str) -> list[dict[str, Any]]:
    """Read a deployment's recent runs, without assuming run-name conventions."""
    with httpx.Client(base_url=server.prefect_url, timeout=30) as prefect:
        response = _prefect_call(
            prefect,
            "POST",
            "/flow_runs/filter",
            json={"deployments": {"id": {"any_": [deployment_id]}}, "limit": 100},
        )
        response.raise_for_status()
        return response.json()


def states(record: FileRead) -> dict[str, FileState]:
    """Copy states by storage-location name."""
    return {copy.storage_location: copy.state for copy in record.storage_locations}


def upload(
    client: HelioPuddleClient, tmp_path: Path, path: str, data: bytes = b"content"
) -> FileRead:
    """Upload a small local source through the public client."""
    source = tmp_path / "source.bin"
    source.write_bytes(data)
    return client.add_file(source, path)

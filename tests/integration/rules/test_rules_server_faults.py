"""Rules server acceptance: quota, killed transfers and racing triggers."""

from __future__ import annotations

import signal
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import create_engine, text

from heliopuddle.client import HelioPuddleClient
from heliopuddle.server.schemas import FileState
from tests.integration.rules.helpers import (  # noqa: F401
    FINAL,
    PROJECT,
    TIMEOUT,
    deployment,
    runs_for,
    states,
    upload,
    wait_run,
)

from ..conftest import Deployment
from ..test_postgres_server import _prefect_call


@pytest.mark.req("STO-03", "STO-04", "STO-14", "SVC-21")
def test_azurite_quota_failure_preserves_move_source(
    rules_server: Deployment, rules_client: HelioPuddleClient, tmp_path: Path
) -> None:
    content = b"x" * 1_000_001
    record = upload(rules_client, tmp_path, "quota/too-large.dat", content)
    run = rules_client.run_once(
        {
            "action": "move",
            "source": "primary",
            "destination": "archive",
            "match": {"paths": [record.path]},
            "options": {"check_for_space_overhead": "0B"},
        }
    )
    assert wait_run(rules_server, run.flow_run_id)["state"]["type"] == "FAILED"
    assert (rules_server.store / record.path).read_bytes() == content
    assert states(rules_client.get_file(record.id))["primary"] == FileState.AVAILABLE


@pytest.mark.req("STO-03", "SVC-19")
def test_killed_transfer_is_repaired_without_losing_source(
    rules_server: Deployment, rules_client: HelioPuddleClient, tmp_path: Path
) -> None:
    content = b"interrupted transfer\n" * 100
    record = upload(rules_client, tmp_path, "interrupted/source.dat", content)
    script = """
import os
import signal
from contextlib import nullcontext
from io import BytesIO
from prefect import flow
from heliopuddle.config.models import RuleOptions
from heliopuddle.server.services import transfer
from heliopuddle.storage import StorageLocation

original = StorageLocation.write
def interrupted_write(self, path, source, **kwargs):
    original(self, path, BytesIO(source.read(32)), **kwargs)
    os.kill(os.getpid(), signal.SIGKILL)

StorageLocation.write = interrupted_write
# The ownership/recovery test must not wait for a killed Prefect limit lease.
# Actual shared-limit behavior is covered against Prefect separately.
transfer.hp_concurrency = lambda *args, **kwargs: nullcontext()

@flow(name="m4_interrupted_transfer")
def interrupted():
    transfer.transfer_file(
        int(os.environ["HP_TEST_FILE_ID"]), "primary", "archive",
        action="move", options=RuleOptions(check_for_space_overhead=0),
    )
interrupted()
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={**rules_server.process.env, "HP_TEST_FILE_ID": str(record.id)},
        capture_output=True,
        text=True,
        start_new_session=True,
        timeout=120,
    )
    assert result.returncode == -signal.SIGKILL, result.stdout + result.stderr
    assert (rules_server.store / record.path).read_bytes() == content
    assert states(rules_client.get_file(record.id))["primary"] == FileState.AVAILABLE
    assert rules_server.database_url is not None
    engine = create_engine(rules_server.database_url)
    try:
        with engine.begin() as conn:
            owned = (
                conn.execute(
                    text(
                        "UPDATE file_storage_location SET op_deadline = :at "
                        "WHERE file_id = :id AND op_token IS NOT NULL RETURNING id"
                    ),
                    {"at": datetime.now(UTC) - timedelta(seconds=1), "id": record.id},
                )
                .scalars()
                .all()
            )
            assert len(owned) == 1
        run = rules_client.trigger_index("archive", mode="full")
        assert wait_run(rules_server, run.flow_run_id)["state"]["type"] == "COMPLETED"
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text(
                        "SELECT count(*) FROM file_storage_location "
                        "WHERE file_id = :id AND op_token IS NOT NULL"
                    ),
                    {"id": record.id},
                ).scalar_one()
                == 0
            )
    finally:
        engine.dispose()
    assert (rules_server.store / record.path).read_bytes() == content
    assert states(rules_client.get_file(record.id)) == {"primary": FileState.AVAILABLE}


@pytest.mark.req("SVC-24", "SVC-25", "API-14")
def test_racing_triggers_create_one_run_and_disable_cancels_future_runs(
    rules_server: Deployment, rules_client: HelioPuddleClient
) -> None:
    from prefect.settings import PREFECT_API_URL, temporary_settings

    from heliopuddle.server.services.concurrency import hp_concurrency

    rule = rules_client.get_rule("trigger-race")
    dep = deployment(rules_server, rule.deployment)
    existing = {run["id"] for run in runs_for(rules_server, dep["id"])}
    barrier = threading.Barrier(2)

    def trigger() -> dict[str, Any]:
        barrier.wait(timeout=10)
        response = httpx.post(
            f"{rules_server.api_url}/projects/{PROJECT}/rules/trigger-race/trigger", timeout=60
        )
        assert response.status_code == 200, response.text
        return response.json()

    with temporary_settings({PREFECT_API_URL: rules_server.prefect_url}):
        with hp_concurrency(f"rule:{PROJECT}:trigger-race", strict=True, timeout_seconds=30):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: trigger(), range(2)))
            assert sum(result["started"] for result in results) == 1, results
            new_runs = [
                run for run in runs_for(rules_server, dep["id"]) if run["id"] not in existing
            ]
            assert len(new_runs) == 1
            assert not rules_client.trigger_rule("trigger-race").started
        assert wait_run(rules_server, new_runs[0]["id"])["state"]["type"] == "COMPLETED"

    scheduled = rules_client.get_rule("scheduled-control")
    scheduled_dep = deployment(rules_server, scheduled.deployment)
    with httpx.Client(base_url=rules_server.prefect_url, timeout=30) as prefect:
        response = _prefect_call(
            prefect,
            "POST",
            f"/deployments/{scheduled_dep['id']}/create_flow_run",
            json={
                "state": {
                    "type": "SCHEDULED",
                    "name": "Scheduled",
                    "state_details": {
                        "scheduled_time": (datetime.now(UTC) + timedelta(hours=1)).isoformat()
                    },
                }
            },
        )
        response.raise_for_status()
        future_id = response.json()["id"]
    try:
        disabled = rules_client.disable_rule("scheduled-control")
        assert not disabled.effective_enabled
        paused = deployment(rules_server, scheduled.deployment)
        assert paused["paused"] or all(not schedule["active"] for schedule in paused["schedules"])
        assert wait_run(rules_server, future_id)["state"]["type"] == "CANCELLED"
        response = httpx.post(
            f"{rules_server.api_url}/projects/{PROJECT}/rules/scheduled-control/trigger",
            timeout=30,
        )
        assert response.status_code == 409
    finally:
        assert rules_client.enable_rule("scheduled-control").effective_enabled
    resumed = deployment(rules_server, scheduled.deployment)
    assert not resumed["paused"] and all(schedule["active"] for schedule in resumed["schedules"])

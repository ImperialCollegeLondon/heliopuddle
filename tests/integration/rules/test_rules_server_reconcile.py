"""Rules server acceptance: the reconcile deployment and downstream rules."""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from adlfs import AzureBlobFileSystem
from sqlalchemy import create_engine, text

from heliopuddle.client import HelioPuddleClient
from heliopuddle.client.client import HelioPuddleError
from heliopuddle.server.schemas import FileState
from tests.integration.rules.helpers import (
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


@pytest.mark.req("SVC-27", "API-04", "CLI-10")
def test_reconcile_deployment_is_registered_and_refuses_a_concurrent_submission(
    rules_server: Deployment, rules_client: HelioPuddleClient
) -> None:
    dep = deployment(rules_server, f"reconcile/reconcile-p{project_id(rules_server)}-{PROJECT}")
    assert dep["entrypoint"] == "heliopuddle.server.services.reconcile.reconcile"
    assert dep["schedules"] == []
    assert dep["parameters"] == {"project": PROJECT, "verify": False, "rules": None}
    assert dep["concurrency_options"]["collision_strategy"] == "ENQUEUE"
    with pytest.raises(HelioPuddleError) as unknown:
        rules_client.reconcile(rules=["missing"])
    assert unknown.value.status_code == 422

    # An unfinished (here: scheduled) run makes a submission a 409 naming that run.
    with httpx.Client(base_url=rules_server.prefect_url, timeout=30) as prefect:
        response = _prefect_call(
            prefect,
            "POST",
            f"/deployments/{dep['id']}/create_flow_run",
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
        pending_id = response.json()["id"]
        try:
            with pytest.raises(HelioPuddleError) as busy:
                rules_client.reconcile()
            assert busy.value.status_code == 409
            assert pending_id in str(busy.value.detail)
        finally:
            cancel = _prefect_call(
                prefect,
                "POST",
                f"/flow_runs/{pending_id}/set_state",
                json={"state": {"type": "CANCELLED", "name": "Cancelled"}, "force": True},
            )
            cancel.raise_for_status()
    wait_run(rules_server, pending_id)

    run = rules_client.reconcile()
    assert run.deployment == f"reconcile/reconcile-p{project_id(rules_server)}-{PROJECT}"
    with pytest.raises(HelioPuddleError) as second:
        rules_client.reconcile()
    assert second.value.status_code == 409
    assert run.flow_run_id in str(second.value.detail)
    assert wait_run(rules_server, run.flow_run_id)["state"]["type"] == "COMPLETED"


def project_id(server: Deployment) -> int:
    """The project's DB id, as embedded in its deployment names."""
    assert server.database_url is not None
    engine = create_engine(server.database_url)
    try:
        with engine.connect() as conn:
            return int(
                conn.execute(
                    text("SELECT id FROM project WHERE name = :n"), {"n": PROJECT}
                ).scalar_one()
            )
    finally:
        engine.dispose()


@pytest.mark.req("SVC-24")
@pytest.mark.slow
@pytest.mark.parametrize("publication", ["transfer", "repair"])
def test_archive_publication_triggers_downstream_rule(
    rules_server: Deployment,
    rules_client: HelioPuddleClient,
    tmp_path: Path,
    publication: str,
) -> None:
    content = b"a committed archive copy"
    record = upload(rules_client, tmp_path, f"notifications/{publication}.dat", content)
    rule = rules_client.get_rule("archive-notifications")
    dep = deployment(rules_server, rule.deployment)
    before = {run["id"] for run in runs_for(rules_server, dep["id"])}
    if publication == "transfer":
        copied = rules_client.run_once(
            {
                "action": "copy",
                "source": "primary",
                "destination": "archive",
                "match": {"paths": [record.path]},
                "options": {"check_for_space_overhead": "0B"},
            }
        )
        assert wait_run(rules_server, copied.flow_run_id)["state"]["type"] == "COMPLETED"
    else:
        response = httpx.post(
            f"{rules_server.api_url}/projects/{PROJECT}/uploads",
            json={
                "path": record.path,
                "hash": record.hash,
                "size": record.size,
                "storage_location": "archive",
            },
            timeout=30,
        )
        response.raise_for_status()
        reserved = response.json()
        archive = rules_client.get_storage_location("archive")
        fs = AzureBlobFileSystem(**archive.storage_options)
        fs.pipe(archive.url.split("://", 1)[1] + "/" + reserved["write_path"], content)
        assert rules_server.database_url is not None
        engine = create_engine(rules_server.database_url)
        try:
            with engine.begin() as conn:
                copy_id = conn.execute(
                    text(
                        "UPDATE file_storage_location SET op_deadline = :at "
                        "WHERE op_token = :token RETURNING id"
                    ),
                    {
                        "at": datetime.now(UTC) - timedelta(seconds=1),
                        "token": reserved["token"],
                    },
                ).scalar_one()
        finally:
            engine.dispose()
        script = """
import os
import threading
from heliopuddle.server.services.upload import RepairResult, repair_expired

assert repair_expired(int(os.environ["HP_TEST_COPY_ID"])) == RepairResult.REPAIRED
for thread in threading.enumerate():
    if thread.name == "hp-rule-notify":
        thread.join(30)
        assert not thread.is_alive()
"""
        repaired = subprocess.run(
            [sys.executable, "-c", script],
            env={**rules_server.process.env, "HP_TEST_COPY_ID": str(copy_id)},
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert repaired.returncode == 0, repaired.stdout + repaired.stderr
    deadline = time.monotonic() + TIMEOUT
    triggered: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        triggered = [run for run in runs_for(rules_server, dep["id"]) if run["id"] not in before]
        if triggered and all(run["state"]["type"] in FINAL for run in triggered):
            break
        time.sleep(0.5)
    assert len(triggered) == 1, triggered
    assert triggered[0]["state"]["type"] == "COMPLETED"
    assert states(rules_client.get_file(record.id)) == {
        "primary": FileState.AVAILABLE,
        "archive": FileState.DELETED,
    }
    assert (rules_server.store / record.path).read_bytes() == content

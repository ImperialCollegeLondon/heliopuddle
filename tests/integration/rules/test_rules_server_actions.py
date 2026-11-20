"""Rules server acceptance: configuration, moves, ignore and dry runs."""

from __future__ import annotations

import hashlib
import os
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
from heliopuddle.server.schemas import FileRead, FileState
from tests.integration.rules.helpers import (  # noqa: F401
    FINAL,
    PROJECT,
    TIMEOUT,
    deployment,
    states,
    upload,
    wait_run,
)

from ..conftest import Deployment


def run_when_idle(server: Deployment, client: HelioPuddleClient, rule: str, **kwargs: Any) -> Any:
    """Start a manual run, first waiting for any unfinished run it is refused for (409)."""
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        try:
            return client.run_rule(rule, **kwargs)
        except HelioPuddleError as error:
            detail = str(error.detail)
            if error.status_code != 409 or "unfinished run" not in detail:
                raise
            wait_run(server, detail.rsplit(" ", 1)[-1])
    pytest.fail(f"rule {rule!r} never became idle:\n{server.process.log()[-8000:]}")


def wait_archived(client: HelioPuddleClient, file_id: int) -> FileRead:
    """Wait for an automatically triggered move, never submitting a manual run."""
    deadline = time.monotonic() + TIMEOUT
    record = client.get_file(file_id)
    while time.monotonic() < deadline:
        record = client.get_file(file_id)
        if states(record) == {"primary": FileState.DELETED, "archive": FileState.AVAILABLE}:
            return record
        time.sleep(0.5)
    pytest.fail(f"file {file_id} was not archived automatically: {record.model_dump()}")


def age_source(server: Deployment, record: FileRead) -> None:
    """Advance the observed source age deterministically, without waiting a minute."""
    old = datetime.now(UTC) - timedelta(minutes=2)
    os.utime(server.store / record.path, (old.timestamp(), old.timestamp()))
    assert server.database_url is not None
    engine = create_engine(server.database_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE file_storage_location SET mtime = :at WHERE file_id = :id"),
                {"at": old, "id": record.id},
            )
    finally:
        engine.dispose()


@pytest.mark.req("API-14", "SVC-21", "SVC-25", "CLI-09")
def test_rule_configuration_and_disabled_yaml_on_real_prefect(
    rules_server: Deployment, rules_client: HelioPuddleClient
) -> None:
    rules = {rule.name: rule for rule in rules_client.list_rules()}
    assert set(rules) == {
        "archive-old",
        "scheduled-control",
        "archive-notifications",
        "trigger-race",
        "yaml-disabled",
    }
    rule = rules["archive-old"]
    dep = deployment(rules_server, rule.deployment)
    assert dep["entrypoint"] == "heliopuddle.server.services.rules.run_rule"
    assert dep["schedules"] == []
    assert dep["concurrency_options"]["collision_strategy"] == "ENQUEUE"
    scheduled = deployment(rules_server, rules["scheduled-control"].deployment)
    assert [schedule["schedule"]["cron"] for schedule in scheduled["schedules"]] == ["0 0 1 1 *"]
    assert rule.options.batch.max_files == 2
    assert rule.effective_enabled
    assert rules_client.get_storage_location("archive").quota == 1_000_000
    disabled = rules_client.enable_rule("yaml-disabled")
    assert not disabled.effective_enabled
    response = httpx.post(
        f"{rules_server.api_url}/projects/{PROJECT}/runs",
        json={"kind": "rule", "rule": "yaml-disabled"},
        timeout=30,
    )
    assert response.status_code == 409


@pytest.mark.req("SVC-04", "SVC-05", "SVC-19", "SVC-24", "STO-03")
def test_latest_flip_triggers_verified_move_to_azurite(
    rules_server: Deployment, rules_client: HelioPuddleClient, tmp_path: Path
) -> None:
    first = upload(rules_client, tmp_path, "versions/automatic_v1.dat", b"first revision")
    age_source(rules_server, first)
    second = upload(rules_client, tmp_path, "versions/automatic_v2.dat", b"second revision")

    moved = wait_archived(rules_client, first.id)

    assert not moved.is_latest
    assert rules_client.get_file(second.id).is_latest
    assert not (rules_server.store / first.path).exists()
    archive = rules_client.get_storage_location("archive")
    fs = AzureBlobFileSystem(**archive.storage_options)
    archive_path = archive.url.split("://", 1)[1] + "/" + first.path
    assert fs.cat_file(archive_path) == b"first revision"
    assert moved.hash == hashlib.md5(b"first revision").hexdigest()


@pytest.mark.req("API-15", "CLI-09", "SVC-04", "SVC-24")
@pytest.mark.slow
def test_ignored_file_stays_put_then_unignore_triggers_move(
    rules_server: Deployment, rules_client: HelioPuddleClient, tmp_path: Path
) -> None:
    first = upload(rules_client, tmp_path, "versions/ignored_v1.dat", b"ignored revision")
    ignored = rules_client.set_rules_ignored(first.id, True)
    age_source(rules_server, first)
    upload(rules_client, tmp_path, "versions/ignored_v2.dat", b"latest revision")
    # The uploads triggered change runs; a manual run waits for them (409 while unfinished).
    run = run_when_idle(rules_server, rules_client, "archive-old", mode="full")
    assert wait_run(rules_server, run.flow_run_id)["state"]["type"] == "COMPLETED"
    assert (rules_server.store / first.path).exists()
    assert states(rules_client.get_file(first.id)) == {"primary": FileState.AVAILABLE}
    assert [f.id for f in rules_client.find(ignored_from_all_rules=True).items] == [first.id]

    restored = rules_client.set_rules_ignored(first.path, False)

    assert restored.updated_by == "m4-test"
    assert restored.updated_at > ignored.updated_at
    wait_archived(rules_client, first.id)


@pytest.mark.req("API-04", "SVC-20", "SVC-21", "STO-03", "CLI-09")
@pytest.mark.slow
def test_one_off_dry_run_totals_and_source_only_delete(
    rules_server: Deployment, rules_client: HelioPuddleClient, tmp_path: Path
) -> None:
    files = [
        upload(rules_client, tmp_path, f"one-off/{index}.dat", f"content {index}".encode())
        for index in range(3)
    ]
    spec: dict[str, Any] = {
        "action": "copy",
        "source": "primary",
        "destination": "archive",
        "match": {"paths": ["one-off/**"]},
        "options": {
            "check_for_space_overhead": "0B",
            "batch": {"max_files": 1},
            "dry_run": True,
        },
    }
    before = [rules_client.get_file(file.id).model_dump() for file in files]
    dry = rules_client.run_once(spec)
    assert wait_run(rules_server, dry.flow_run_id)["state"]["type"] == "COMPLETED"
    assert [rules_client.get_file(file.id).model_dump() for file in files] == before

    spec["options"]["dry_run"] = False
    real = rules_client.run_once(spec)
    assert wait_run(rules_server, real.flow_run_id)["state"]["type"] == "COMPLETED"
    copied = [file for file in files if "archive" in states(rules_client.get_file(file.id))]
    assert len(copied) == 1
    victim = copied[0]
    deleted = rules_client.run_once(
        {"action": "delete", "source": "primary", "match": {"paths": [victim.path]}}
    )
    assert wait_run(rules_server, deleted.flow_run_id)["state"]["type"] == "COMPLETED"
    after = rules_client.get_file(victim.id)
    assert after.deleted_at is None
    assert states(after) == {"primary": FileState.DELETED, "archive": FileState.AVAILABLE}
    assert not (rules_server.store / victim.path).exists()
    assert len(rules_client.list_rules()) == 5

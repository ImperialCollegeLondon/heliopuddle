"""Fixtures for the rules server acceptance tests (one server for the package)."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

import httpx
import pytest
import yaml

from heliopuddle.client import HelioPuddleClient

from ..conftest import Deployment, base_env, free_port, start_hp
from ..test_postgres_server import _prefect_call
from .helpers import PROJECT, wait_run


@pytest.fixture(scope="package")
def rules_server(
    tmp_path_factory: pytest.TempPathFactory,
    postgres_database: Callable[[], str],
    external_prefect: str,
    azurite: str,
) -> Iterator[Deployment]:
    """A real two-storage-location service with scheduled and on-demand rules."""
    root = tmp_path_factory.mktemp("rules-server")
    store, config_dir, data_dir, logs = (
        root / name for name in ("store", "config", "data", "logs")
    )
    for path in (store, config_dir, data_dir, logs):
        path.mkdir()
    common = {"check_for_space_overhead": "0B", "pause_between_runs": "1s"}
    rules = [
        {
            "name": "archive-old",
            "action": "move",
            "source": "primary",
            "destination": "archive",
            "match": {
                "paths": ["versions/**"],
                "is_latest": False,
                "age": {"modified": {"older_than": "1m"}},
            },
            "options": {**common, "batch": {"max_files": 2}},
        },
        {
            "name": "scheduled-control",
            "action": "copy",
            "source": "primary",
            "destination": "archive",
            "match": {"paths": ["scheduled/**"]},
            "options": common,
            "schedule": "0 0 1 1 *",
        },
        {
            "name": "archive-notifications",
            "action": "delete",
            "source": "archive",
            "match": {"paths": ["notifications/**"]},
            "options": common,
        },
        {
            "name": "trigger-race",
            "action": "copy",
            "source": "primary",
            "destination": "archive",
            "match": {"paths": ["race/**"]},
            "options": common,
        },
        {
            "name": "yaml-disabled",
            "action": "delete",
            "source": "primary",
            "match": {"paths": ["disabled/**"]},
            "enabled": False,
        },
    ]
    config = {
        "project": PROJECT,
        "storage_locations": [
            {"name": "primary", "url": store.as_uri()},
            {
                "name": "archive",
                "url": f"az://hp-rules-{uuid.uuid4().hex[:12]}/archive",
                "storage_options": {"connection_string": "${HP_TEST_ARCHIVE_CONN}"},
                "create_root": True,
                "quota": "1MB",
            },
        ],
        "index": {
            "settle_time": "0s",
            "batch_size": 100,
            "batch": {"max_files": 100},
            "pause_between_runs": "1s",
        },
        "rules": rules,
    }
    (config_dir / "rules.yaml").write_text(yaml.safe_dump(config))
    database_url = postgres_database()
    port = free_port()
    env = base_env(
        HELIOPUDDLE_CONFIG_DIR=str(config_dir),
        HELIOPUDDLE_DATA_DIR=str(data_dir),
        HELIOPUDDLE_DATABASE_URL=database_url,
        HELIOPUDDLE_PORT=str(port),
        PREFECT_API_URL=external_prefect,
        PREFECT_HOME=str(data_dir / "prefect-client"),
        HP_TEST_ARCHIVE_CONN=azurite,
    )
    process = start_hp(["server", "start"], env, logs / "server.log")
    server = Deployment(
        process=process,
        base_url=f"http://127.0.0.1:{port}",
        prefect_url=external_prefect,
        store=store,
        config_dir=config_dir,
        data_dir=data_dir,
        logs=logs,
        database_url=database_url,
    )
    try:
        process.wait_for(f"{server.api_url}/health")
        with httpx.Client(base_url=server.prefect_url, timeout=30) as prefect:
            response = _prefect_call(
                prefect,
                "POST",
                "/flow_runs/filter",
                json={"flow_runs": {"tags": {"all_": ["heliopuddle-adoption"]}}},
            )
            response.raise_for_status()
            for run in response.json():
                if run["parameters"].get("project") == PROJECT:
                    assert wait_run(server, run["id"])["state"]["type"] == "COMPLETED"
        yield server
    finally:
        process.stop()


@pytest.fixture
def rules_client(rules_server: Deployment) -> Iterator[HelioPuddleClient]:
    """The public HTTP/storage client against the running service."""
    with HelioPuddleClient(rules_server.api_url, PROJECT, user="m4-test") as client:
        yield client

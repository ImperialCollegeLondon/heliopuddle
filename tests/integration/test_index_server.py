"""SVC-09: index deployments, schedules, adoption on start and the storage-location lock.

A server of its own (embedded Prefect, SQLite) starts with a store that
already holds versioned files and a project with index schedules. These tests
need the index flows (``heliopuddle.server.services.index``).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from heliopuddle.server.services.deploy import ADOPTION_TAG
from heliopuddle.server.services.index_names import (
    index_deployment_full_name,
    storage_location_lock,
)

from .conftest import Deployment, base_env, free_port, start_hp

pytestmark = pytest.mark.req("SVC-09")

PROJECT = "adopt"
RUN_TIMEOUT_S = 180.0
FINAL = {"COMPLETED", "FAILED", "CRASHED", "CANCELLED"}
EXISTING = {
    "data/2026/obs_v001.csv": b"version one\n",
    "data/2026/obs_v002.csv": b"version two\n",
    "notes/readme.txt": b"no version here\n",
}


@pytest.fixture(scope="module")
def hp_index_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Deployment]:
    """``server start`` with pre-existing files and index schedules in the project config."""
    root = tmp_path_factory.mktemp("hp-index")
    store, config_dir, data_dir, logs = (root / n for n in ("store", "config", "data", "logs"))
    for d in (store, config_dir, data_dir, logs):
        d.mkdir()
    for rel, data in EXISTING.items():
        (store / rel).parent.mkdir(parents=True, exist_ok=True)
        (store / rel).write_bytes(data)
    (config_dir / f"{PROJECT}.yaml").write_text(
        f"project: {PROJECT}\n"
        "storage_locations:\n"
        "  - name: primary\n"
        f"    url: file://{store}\n"
        "    storage_options:\n"
        "      auto_mkdir: true\n"
        "index:\n"
        "  full_schedule: '0 3 * * *'\n"
        "  incremental_schedule: '*/10 * * * *'\n"
        "  settle_time: 0s\n"
    )
    # Older than the settle time anyway: the files were written before start.
    port, prefect_port = free_port(), free_port()
    env = base_env(
        HELIOPUDDLE_CONFIG_DIR=str(config_dir),
        HELIOPUDDLE_DATA_DIR=str(data_dir),
        PREFECT_SERVER_API_PORT=str(prefect_port),
        PREFECT_HOME=str(data_dir / "parent-prefect"),
    )
    process = start_hp(["server", "start", "--port", str(port)], env, logs / "server.log")
    try:
        deployment = Deployment(
            process=process,
            base_url=f"http://127.0.0.1:{port}",
            prefect_url=f"http://127.0.0.1:{prefect_port}/api",
            store=store,
            config_dir=config_dir,
            data_dir=data_dir,
            logs=logs,
        )
        process.wait_for(f"{deployment.api_url}/health")
        yield deployment
    finally:
        process.stop()


def _prefect(server: Deployment) -> httpx.Client:
    return httpx.Client(base_url=server.prefect_url, timeout=30)


def _deployment(server: Deployment, mode: str) -> dict[str, Any]:
    name = index_deployment_full_name(mode, 1, 1, PROJECT, "primary")  # type: ignore[arg-type]
    r = _prefect(server).get(f"/deployments/name/{name}")
    assert r.status_code == 200, f"{name}: {r.text}\n{server.process.log()[-5000:]}"
    return r.json()


def _wait_final(server: Deployment, run_id: str, timeout: float = RUN_TIMEOUT_S) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    run: dict[str, Any] = {}
    while time.monotonic() < deadline:
        run = _prefect(server).get(f"/flow_runs/{run_id}").json()
        if run["state"]["type"] in FINAL:
            return run
        time.sleep(1)
    pytest.fail(f"flow run {run_id} not final: {run.get('state')}\n{server.process.log()[-5000:]}")


def _submit(server: Deployment, mode: str) -> str:
    dep = _deployment(server, mode)
    r = _prefect(server).post(f"/deployments/{dep['id']}/create_flow_run", json={})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _artifacts_text(server: Deployment, run_id: str) -> str:
    r = _prefect(server).post(
        "/artifacts/filter", json={"artifacts": {"flow_run_id": {"any_": [run_id]}}}
    )
    assert r.status_code == 200, r.text
    return "\n".join(str(a.get("data") or "") for a in r.json())


def test_index_deployments_are_registered_with_schedules(hp_index_server: Deployment) -> None:
    full = _deployment(hp_index_server, "full")
    incremental = _deployment(hp_index_server, "incremental")

    assert full["name"] == f"index-full-p1-sl1-{PROJECT}-primary"
    assert full["parameters"] == {"project": PROJECT, "storage_location": "primary"}
    assert full["entrypoint"] == "heliopuddle.server.services.index.index_full"
    assert [s["schedule"]["cron"] for s in full["schedules"]] == ["0 3 * * *"]
    assert incremental["entrypoint"] == "heliopuddle.server.services.index.index_incremental"
    assert [s["schedule"]["cron"] for s in incremental["schedules"]] == ["*/10 * * * *"]
    limit = _prefect(hp_index_server).get(
        f"/v2/concurrency_limits/{storage_location_lock(PROJECT, 'primary')}"
    )
    assert limit.status_code == 200, limit.text
    assert limit.json()["limit"] == 1


def _adoption_runs(server: Deployment) -> list[dict[str, Any]]:
    dep = _deployment(server, "full")
    r = _prefect(server).post(
        "/flow_runs/filter",
        json={
            "deployments": {"id": {"any_": [dep["id"]]}},
            "flow_runs": {"tags": {"all_": [ADOPTION_TAG]}},
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _wait_adoption(server: Deployment) -> None:
    """Wait for the one adoption run submitted on start; it must complete."""
    runs = [r for r in _adoption_runs(server) if r["idempotency_key"] == "adopt-sl1-g1"]
    assert len(runs) == 1, runs
    assert runs[0]["parameters"] == {"project": PROJECT, "storage_location": "primary"}
    run = _wait_final(server, runs[0]["id"])
    assert run["state"]["type"] == "COMPLETED", server.process.log()[-5000:]


def test_adoption_run_completes_on_start(hp_index_server: Deployment) -> None:
    _wait_adoption(hp_index_server)

    files = httpx.get(
        f"{hp_index_server.api_url}/projects/{PROJECT}/files", params={"limit": 100}, timeout=30
    ).json()
    by_path = {f["path"]: f for f in files["items"]}
    assert set(by_path) == set(EXISTING)
    assert by_path["data/2026/obs_v002.csv"]["is_latest"]
    assert not by_path["data/2026/obs_v001.csv"]["is_latest"]
    for f in by_path.values():
        [copy] = f["storage_locations"]
        assert copy["state"] == "available"


def test_incremental_skips_and_full_waits_while_the_lock_is_held(
    hp_index_server: Deployment,
) -> None:
    from prefect.settings import PREFECT_API_URL, temporary_settings

    from heliopuddle.server.services.concurrency import hp_concurrency

    # Let the adoption run finish first so it does not hold the lock.
    _wait_adoption(hp_index_server)
    lock = storage_location_lock(PROJECT, "primary")
    with temporary_settings({PREFECT_API_URL: hp_index_server.prefect_url}):
        with hp_concurrency(lock, timeout_seconds=30, strict=True):
            full_id = _submit(hp_index_server, "full")
            incremental_id = _submit(hp_index_server, "incremental")
            incremental = _wait_final(hp_index_server, incremental_id)
            full_state = _prefect(hp_index_server).get(f"/flow_runs/{full_id}").json()["state"]
        full = _wait_final(hp_index_server, full_id)

    assert incremental["state"]["type"] == "COMPLETED"
    assert "skipped" in _artifacts_text(hp_index_server, incremental_id).lower()
    assert full_state["type"] not in FINAL  # it was waiting for the lock
    assert full["state"]["type"] == "COMPLETED"


def test_a_second_process_does_not_adopt_again(hp_index_server: Deployment, tmp_path: Path) -> None:
    # A second process against the same Prefect, database and config finds the
    # storage location fully indexed (index_progress.last_full_at is set) and
    # submits no adoption run.
    env = base_env(
        HELIOPUDDLE_CONFIG_DIR=str(hp_index_server.config_dir),
        HELIOPUDDLE_DATA_DIR=str(hp_index_server.data_dir),
        PREFECT_API_URL=hp_index_server.prefect_url,
        PREFECT_HOME=str(tmp_path / "prefect"),
    )
    _wait_adoption(hp_index_server)
    proc = start_hp(["server", "start", "--no-api", "--no-ui"], env, tmp_path / "services.log")
    try:
        deadline = time.monotonic() + 120
        while "Adoption runs submitted" not in proc.log() and time.monotonic() < deadline:
            assert proc.proc.poll() is None, proc.log()
            time.sleep(0.5)
        assert "Adoption runs submitted: 0" in proc.log(), proc.log()
    finally:
        proc.stop()


@pytest.mark.req("SVC-12", "SVC-18")
@pytest.mark.slow
def test_two_processes_starting_together_submit_one_adoption_run(
    hp_index_server: Deployment, tmp_path: Path
) -> None:
    # Index progress is reset (as a storage URL change does), then two
    # processes start at the same moment against the same Prefect, database
    # and config: both race to create the limits and both submit the
    # adoption run, with the same idempotency key, so Prefect creates one.
    import sqlite3

    _wait_adoption(hp_index_server)
    with sqlite3.connect(hp_index_server.data_dir / "heliopuddle.db") as db:
        db.execute("DELETE FROM index_progress")
    procs = []
    for i in range(2):
        env = base_env(
            HELIOPUDDLE_CONFIG_DIR=str(hp_index_server.config_dir),
            HELIOPUDDLE_DATA_DIR=str(hp_index_server.data_dir),
            PREFECT_API_URL=hp_index_server.prefect_url,
            PREFECT_HOME=str(tmp_path / f"prefect{i}"),
        )
        procs.append(
            start_hp(["server", "start", "--no-api", "--no-ui"], env, tmp_path / f"s{i}.log")
        )
    try:
        deadline = time.monotonic() + 180
        for proc in procs:
            while "Adoption runs submitted" not in proc.log() and time.monotonic() < deadline:
                assert proc.proc.poll() is None, proc.log()
                time.sleep(0.5)
            assert "Adoption runs submitted" in proc.log(), proc.log()
        keys = sorted(r["idempotency_key"] for r in _adoption_runs(hp_index_server))
        assert keys == ["adopt-sl1-g1", "adopt-sl1-g2"], keys
        [second] = [
            r for r in _adoption_runs(hp_index_server) if r["idempotency_key"].endswith("g2")
        ]
        assert _wait_final(hp_index_server, second["id"])["state"]["type"] == "COMPLETED"
    finally:
        for proc in procs:
            proc.stop()

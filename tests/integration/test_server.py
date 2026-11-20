"""``heliopuddle server start`` as a real process (SVC-01, SVC-02, DEP-02, UI-03).

Tests that stop the shared server come last in this file.
"""

from __future__ import annotations

import hashlib
import signal
import sqlite3
import time
from pathlib import Path

import httpx
import pytest

from heliopuddle.client import HelioPuddleClient
from heliopuddle.server.schemas import FileState

from .conftest import PROJECT, Deployment, HpProcess, base_env, free_port, start_hp

FLOW_RUN_TIMEOUT_S = 120.0


def _add(hp_server: Deployment, tmp_path: Path, path: str, data: bytes) -> int:
    local = tmp_path / Path(path).name
    local.write_bytes(data)
    with HelioPuddleClient(hp_server.api_url, PROJECT, user="itest") as client:
        record = client.add_file(local, path)
    assert record.state == FileState.AVAILABLE
    assert (hp_server.store / path).read_bytes() == data
    return record.id


def _copy_ids(hp_server: Deployment, file_ids: list[int]) -> list[int]:
    marks = ",".join("?" * len(file_ids))
    with sqlite3.connect(hp_server.db_path) as conn:
        rows = conn.execute(
            f"SELECT id FROM file_storage_location WHERE file_id IN ({marks}) ORDER BY id",
            file_ids,
        ).fetchall()
    return [r[0] for r in rows]


def _set_copy_states(hp_server: Deployment, copy_ids: list[int], state: str) -> None:
    marks = ",".join("?" * len(copy_ids))
    with sqlite3.connect(hp_server.db_path, timeout=30) as conn:
        conn.execute(
            f"UPDATE file_storage_location SET state = ? WHERE id IN ({marks})",
            [state, *copy_ids],
        )


# --- DEP-02 / UI-03 (a): everything in one process, one port ----------------------


@pytest.mark.req("DEP-02")
@pytest.mark.req("SVC-01")
def test_health_reports_database_and_embedded_prefect(hp_server: Deployment) -> None:
    body = httpx.get(f"{hp_server.api_url}/health", timeout=10).json()
    assert body == {"status": "ok", "database": "ok", "prefect": "ok"}
    assert httpx.get(f"{hp_server.prefect_url}/health", timeout=10).status_code == 200
    assert (hp_server.data_dir / "prefect").is_dir()  # PREFECT_HOME under the data dir


@pytest.mark.req("SVC-01")
def test_verify_files_deployment_is_registered(hp_server: Deployment) -> None:
    r = httpx.get(f"{hp_server.prefect_url}/deployments/name/verify_files/verify-files", timeout=10)
    assert r.status_code == 200, r.text
    assert r.json()["entrypoint"] == "heliopuddle.server.services.verify.verify_files"


@pytest.mark.req("DEP-02")
@pytest.mark.req("UI-03")
def test_client_add_and_ui_on_the_shared_port(hp_server: Deployment, tmp_path: Path) -> None:
    _add(hp_server, tmp_path, "shared/one.txt", b"one\n")

    page = httpx.get(f"{hp_server.base_url}/", timeout=30, follow_redirects=True)

    assert page.status_code == 200
    assert "shared/one.txt" in page.text


@pytest.mark.req("SVC-02")
@pytest.mark.req("SVC-01")
def test_submitted_verify_files_run_completes(hp_server: Deployment, tmp_path: Path) -> None:
    data = {"bulk/a.txt": b"aaa\n", "bulk/b.txt": b"bbb\n"}
    file_ids = [_add(hp_server, tmp_path, p, d) for p, d in data.items()]
    copy_ids = _copy_ids(hp_server, file_ids)
    _set_copy_states(hp_server, copy_ids, "pending")

    prefect = httpx.Client(base_url=hp_server.prefect_url, timeout=30)
    dep = prefect.get("/deployments/name/verify_files/verify-files").json()
    run = prefect.post(
        f"/deployments/{dep['id']}/create_flow_run",
        json={"parameters": {"file_storage_location_ids": copy_ids}},
    )
    assert run.status_code == 201, run.text
    run_id = run.json()["id"]

    deadline = time.monotonic() + FLOW_RUN_TIMEOUT_S
    state = None
    while time.monotonic() < deadline:
        state = prefect.get(f"/flow_runs/{run_id}").json()["state"]["type"]
        if state in {"COMPLETED", "FAILED", "CRASHED", "CANCELLED"}:
            break
        time.sleep(1)
    assert state == "COMPLETED", hp_server.process.log()[-5000:]

    with HelioPuddleClient(hp_server.api_url, PROJECT) as client:
        for file_id, (path, content) in zip(file_ids, data.items(), strict=True):
            record = client.get_file(file_id)
            assert record.path == path
            [copy] = record.storage_locations
            assert copy.state == FileState.AVAILABLE
            assert copy.hash == hashlib.md5(content).hexdigest()


# --- UI-03 (b) and root paths: UI on its own port ------------------------------------


@pytest.mark.req("UI-03")
@pytest.mark.req("DEP-02")
@pytest.mark.req("DB-04")
def test_ui_on_own_port_with_root_paths(
    hp_server: Deployment, run_hp: list[HpProcess], tmp_path: Path
) -> None:
    _add(hp_server, tmp_path, "own-port/two.txt", b"two\n")
    port, ui_port = free_port(), free_port()
    env = hp_server.shared_env(
        HELIOPUDDLE_UI_PORT=str(ui_port),
        HELIOPUDDLE_API_ROOT_PATH="/hp-api",
        HELIOPUDDLE_UI_ROOT_PATH="/hp-ui",
    )
    proc = start_hp(
        ["server", "start", "--port", str(port), "--no-services", "--no-migrate"],
        env,
        hp_server.logs / "own-port.log",
    )
    run_hp.append(proc)
    api, ui = f"http://127.0.0.1:{port}", f"http://127.0.0.1:{ui_port}"

    proc.wait_for(f"{api}/hp-api/api/v1/health", timeout=90)
    # a proxy may strip the prefix or keep it
    assert httpx.get(f"{api}/api/v1/health", timeout=10).status_code == 200
    assert httpx.get(f"{api}/", timeout=10).status_code == 404  # no UI on the API port

    for url in (f"{ui}/", f"{ui}/hp-ui/"):
        page = httpx.get(url, timeout=30, follow_redirects=True)
        assert page.status_code == 200, page.text
        assert "own-port/two.txt" in page.text
        assert "/hp-ui/" in page.text  # links carry the UI root path
    assert httpx.get(f"{ui}/hp-ui/static/hp.css", timeout=10).status_code == 200
    assert httpx.get(f"{ui}/api/v1/health", timeout=10).status_code == 404

    assert proc.stop() == 0


# --- UI-03 (c): a separate UI process ------------------------------------------------


@pytest.mark.req("UI-03")
@pytest.mark.req("DEP-02")
def test_ui_only_process_talks_to_another_api(
    hp_server: Deployment, run_hp: list[HpProcess], tmp_path: Path
) -> None:
    _add(hp_server, tmp_path, "ui-only/three.txt", b"three\n")
    port = free_port()
    env = base_env(
        HELIOPUDDLE_UI_API_URL=hp_server.api_url,
        # no config, no database: a UI-only process needs neither
        HELIOPUDDLE_CONFIG_DIR=str(tmp_path / "no-config"),
        HELIOPUDDLE_DATA_DIR=str(tmp_path / "no-data"),
        PREFECT_API_URL=f"http://127.0.0.1:{free_port()}/api",  # unreachable; never used
    )
    proc = start_hp(
        ["server", "start", "--no-api", "--no-services", "--port", str(port)],
        env,
        hp_server.logs / "ui-only.log",
    )
    run_hp.append(proc)

    page = proc.wait_for(f"http://127.0.0.1:{port}/", timeout=90)

    assert "ui-only/three.txt" in page.text
    assert not (tmp_path / "no-data").exists()
    assert proc.stop() == 0


# --- DEP-02: services only, against an external Prefect ---------------------------------


@pytest.mark.req("DEP-02")
@pytest.mark.req("SVC-01")
def test_services_only_with_external_prefect(
    hp_server: Deployment, run_hp: list[HpProcess]
) -> None:
    port = free_port()
    proc = start_hp(
        ["server", "start", "--no-api", "--no-ui", "--port", str(port)],
        hp_server.shared_env(),
        hp_server.logs / "services-only.log",
    )
    run_hp.append(proc)

    deadline = time.monotonic() + 90
    while "Serving deployments: verify_files/verify-files" not in proc.log():
        assert proc.proc.poll() is None, proc.log()
        assert time.monotonic() < deadline, proc.log()
        time.sleep(0.5)
    with pytest.raises(httpx.ConnectError):
        httpx.get(f"http://127.0.0.1:{port}/api/v1/health", timeout=2)
    assert "Starting the embedded Prefect server" not in proc.log()

    proc.signal(signal.SIGINT)
    proc.proc.wait(30)
    assert proc.proc.returncode == 0, proc.log()
    assert "Stopped" in proc.log()


# --- SVC-01: an unreachable external Prefect stops startup -----------------------------


@pytest.mark.req("SVC-01")
@pytest.mark.req("DEP-02")
def test_unreachable_external_prefect_fails_fast(
    hp_server: Deployment, run_hp: list[HpProcess], tmp_path: Path
) -> None:
    dead = f"http://127.0.0.1:{free_port()}/api"
    env = hp_server.shared_env(PREFECT_API_URL=dead)
    proc = start_hp(
        ["server", "start", "--port", str(free_port())], env, hp_server.logs / "dead.log"
    )
    run_hp.append(proc)

    started = time.monotonic()
    code = proc.proc.wait(60)

    assert code == 1
    assert time.monotonic() - started < 60
    log = proc.log()
    assert f"Prefect API at {dead} (PREFECT_API_URL) is unreachable" in log
    assert "Traceback" not in log


# --- DEP-02 / SVC-01: graceful shutdown (stops the shared server; keep last) -------------


@pytest.mark.req("DEP-02")
@pytest.mark.req("SVC-01")
def test_sigterm_stops_everything_including_embedded_prefect(hp_server: Deployment) -> None:
    proc = hp_server.process
    proc.signal(signal.SIGTERM)
    code = proc.proc.wait(60)

    assert code == 0, proc.log()[-5000:]
    log = proc.log()
    order = [
        log.index("Shutting down (SIGTERM)"),
        log.index("Stopping the served deployments"),
        log.index("Stopping the embedded Prefect server"),
        log.index("Stopped"),
    ]
    assert order == sorted(order)
    with pytest.raises(httpx.ConnectError):
        httpx.get(f"{hp_server.prefect_url}/health", timeout=2)

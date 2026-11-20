"""``server start`` on Postgres with an external Prefect server, by env vars only (DEP-04).

The hp process gets only environment variables (``HELIOPUDDLE_DATABASE_URL``,
``PREFECT_API_URL``, config and data dirs, port). The Prefect container is the
API server only; hp registers and serves the deployments itself.
"""

from __future__ import annotations

import hashlib
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import create_engine, text

from heliopuddle.server.database.migrate import head_revision

from .conftest import HP, PROJECT, Deployment, base_env

USER = "pg-itest"
FLOW_RUN_TIMEOUT_S = 120.0
PREFECT_RETRIES = 5


def _prefect_call(prefect: httpx.Client, method: str, url: str, **kwargs: Any) -> httpx.Response:
    """Call the Prefect API, retrying a 503 (busy database) as Prefect's own client does."""
    response = prefect.request(method, url, **kwargs)
    for attempt in range(PREFECT_RETRIES):
        if response.status_code != 503:
            break
        time.sleep(0.5 * 2**attempt)
        response = prefect.request(method, url, **kwargs)
    return response


def _db_rows(database_url: str, sql: str) -> list[tuple[object, ...]]:
    engine = create_engine(database_url)
    try:
        with engine.connect() as conn:
            return [tuple(r) for r in conn.execute(text(sql))]
    finally:
        engine.dispose()


@pytest.mark.req("DEP-04")
@pytest.mark.req("DB-04")
@pytest.mark.req("DB-01b")
def test_db_upgrade_migrates_postgres(postgres_url: str, tmp_path: Path) -> None:
    env = base_env(
        HELIOPUDDLE_DATABASE_URL=postgres_url,
        HELIOPUDDLE_CONFIG_DIR=str(tmp_path / "config"),
        HELIOPUDDLE_DATA_DIR=str(tmp_path / "data"),
    )

    for _ in range(2):  # a second run finds the schema at head
        result = subprocess.run(
            [HP, "db", "upgrade"], env=env, capture_output=True, text=True, timeout=120
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert f"is at revision {head_revision()}" in result.stdout
        assert "postgresql+psycopg://hp:***@" in result.stdout
        assert "hp-secret" not in result.stdout + result.stderr

    assert _db_rows(postgres_url, "SELECT version_num FROM alembic_version") == [(head_revision(),)]
    assert not (tmp_path / "data" / "heliopuddle.db").exists()


@pytest.mark.req("DEP-04")
def test_health_on_postgres_and_external_prefect(hp_postgres_server: Deployment) -> None:
    body = httpx.get(f"{hp_postgres_server.api_url}/health", timeout=10).json()

    assert body == {"status": "ok", "database": "ok", "prefect": "ok"}
    log = hp_postgres_server.process.log()
    assert "Starting the embedded Prefect server" not in log
    assert "hp-secret" not in log
    assert not (hp_postgres_server.data_dir / "heliopuddle.db").exists()
    assert not (hp_postgres_server.data_dir / "prefect").exists()  # no embedded Prefect home
    assert hp_postgres_server.database_url is not None
    projects = _db_rows(hp_postgres_server.database_url, "SELECT name FROM project")
    assert projects == [(PROJECT,)]


@pytest.mark.req("DEP-04")
@pytest.mark.req("SVC-01")
def test_deployments_registered_with_external_prefect(hp_postgres_server: Deployment) -> None:
    prefect = httpx.Client(base_url=hp_postgres_server.prefect_url, timeout=10)
    expected = {
        "verify_files/verify-files": "heliopuddle.server.services.verify.verify_files",
        "delete_files/delete-files": "heliopuddle.server.services.delete.delete_files",
    }

    for name, entrypoint in expected.items():
        r = prefect.get(f"/deployments/name/{name}")
        assert r.status_code == 200, r.text
        assert r.json()["entrypoint"] == entrypoint
    assert f"Serving deployments: {', '.join(expected)}" in hp_postgres_server.process.log()


@pytest.mark.req("DEP-04")
@pytest.mark.req("DB-01b")
def test_register_on_postgres_ends_available(hp_postgres_server: Deployment) -> None:
    path, data = "pg/one.txt", b"stored in postgres\n"
    target = hp_postgres_server.store / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    digest = hashlib.md5(data).hexdigest()

    r = httpx.post(
        f"{hp_postgres_server.api_url}/projects/{PROJECT}/files",
        json={"path": path, "hash": digest, "size": len(data)},
        headers={"X-Forwarded-User": USER},
        timeout=60,
    )

    assert r.status_code == 201, r.text
    record = r.json()
    assert record["state"] == "available"
    assert record["created_by"] == USER
    [copy] = record["storage_locations"]
    assert copy["hash"] == digest

    got = httpx.get(f"{hp_postgres_server.api_url}/files/{record['id']}", timeout=10).json()
    assert got["state"] == "available"
    assert hp_postgres_server.database_url is not None
    rows = _db_rows(
        hp_postgres_server.database_url,
        "SELECT f.path, f.hash, c.state FROM file f "
        f"JOIN file_storage_location c ON c.file_id = f.id WHERE f.id = {record['id']}",
    )
    assert rows == [(path, digest, "available")]


# --- DEP-04 (full) and SVC-07: versions, removal and deployment runs ----------------------


def _register(server: Deployment, path: str, data: bytes) -> dict[str, Any]:
    """Write ``data`` into the store at ``path`` and register it with a raw POST."""
    target = server.store / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    r = httpx.post(
        f"{server.api_url}/projects/{PROJECT}/files",
        json={"path": path, "hash": hashlib.md5(data).hexdigest(), "size": len(data)},
        headers={"X-Forwarded-User": USER},
        timeout=60,
    )
    assert r.status_code == 201, r.text
    record = r.json()
    assert record["state"] == "available"
    return record


def _get(server: Deployment, file_id: int) -> dict[str, Any]:
    r = httpx.get(f"{server.api_url}/files/{file_id}", timeout=10)
    assert r.status_code == 200, r.text
    return r.json()


def _run_deployment(server: Deployment, name: str, parameters: dict[str, Any]) -> None:
    """Submit a run of deployment ``name`` to the external Prefect; assert it completes."""
    prefect = httpx.Client(base_url=server.prefect_url, timeout=30)
    dep = _prefect_call(prefect, "GET", f"/deployments/name/{name}")
    assert dep.status_code == 200, dep.text
    run = _prefect_call(
        prefect,
        "POST",
        f"/deployments/{dep.json()['id']}/create_flow_run",
        json={"parameters": parameters},
    )
    assert run.status_code == 201, run.text
    run_id = run.json()["id"]
    deadline = time.monotonic() + FLOW_RUN_TIMEOUT_S
    state = None
    while time.monotonic() < deadline:
        state = _prefect_call(prefect, "GET", f"/flow_runs/{run_id}").json()["state"]["type"]
        if state in {"COMPLETED", "FAILED", "CRASHED", "CANCELLED"}:
            break
        time.sleep(1)
    assert state == "COMPLETED", server.process.log()[-5000:]


@pytest.mark.req("DEP-04")
@pytest.mark.req("API-02")
@pytest.mark.req("API-08")
def test_versions_and_delete_on_postgres(hp_postgres_server: Deployment) -> None:
    v1 = _register(hp_postgres_server, "dep04/a_v001.csv", b"one\n")
    v2 = _register(hp_postgres_server, "dep04/a_v002.csv", b"two\n")

    assert v2["series_id"] == v1["series_id"]
    assert v2["is_latest"] is True
    assert _get(hp_postgres_server, v1["id"])["is_latest"] is False

    r = httpx.delete(
        f"{hp_postgres_server.api_url}/files/{v2['id']}",
        headers={"X-Forwarded-User": USER},
        timeout=60,
    )

    assert r.status_code == 200, r.text
    gone = r.json()
    assert gone["deleted_at"] is not None
    assert gone["is_latest"] is False
    assert gone["updated_by"] == USER
    assert [c["state"] for c in gone["storage_locations"]] == ["deleted"]
    assert not (hp_postgres_server.store / "dep04/a_v002.csv").exists()
    assert _get(hp_postgres_server, v1["id"])["is_latest"] is True
    again = httpx.delete(f"{hp_postgres_server.api_url}/files/{v2['id']}", timeout=10)
    assert again.status_code == 404


@pytest.mark.req("DEP-04")
@pytest.mark.req("SVC-02")
def test_verify_files_deployment_run_on_postgres(hp_postgres_server: Deployment) -> None:
    assert hp_postgres_server.database_url is not None
    record = _register(hp_postgres_server, "dep04/verify.csv", b"verify me\n")
    [(copy_id,)] = _db_rows(
        hp_postgres_server.database_url,
        f"SELECT id FROM file_storage_location WHERE file_id = {record['id']}",
    )
    engine = create_engine(hp_postgres_server.database_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE file_storage_location SET state = 'pending' WHERE id = :id"),
                {"id": copy_id},
            )
    finally:
        engine.dispose()
    assert _get(hp_postgres_server, record["id"])["state"] == "pending"

    _run_deployment(
        hp_postgres_server, "verify_files/verify-files", {"file_storage_location_ids": [copy_id]}
    )

    assert _get(hp_postgres_server, record["id"])["state"] == "available"


@pytest.mark.req("SVC-07")
@pytest.mark.req("DEP-04")
def test_delete_files_deployment_run_on_postgres(hp_postgres_server: Deployment) -> None:
    v1 = _register(hp_postgres_server, "svc07/b_v1.dat", b"first\n")
    v2 = _register(hp_postgres_server, "svc07/b_v2.dat", b"second\n")
    assert _get(hp_postgres_server, v2["id"])["is_latest"] is True

    _run_deployment(
        hp_postgres_server, "delete_files/delete-files", {"file_ids": [v2["id"]], "user": USER}
    )

    gone = _get(hp_postgres_server, v2["id"])
    assert gone["deleted_at"] is not None
    assert gone["updated_by"] == USER
    assert [c["state"] for c in gone["storage_locations"]] == ["deleted"]
    assert not (hp_postgres_server.store / "svc07/b_v2.dat").exists()
    assert _get(hp_postgres_server, v1["id"])["is_latest"] is True

"""Integration fixtures: run the real ``heliopuddle server start`` as subprocesses.

One full server (embedded Prefect, API and UI on one port, served
deployments) is shared by the whole session; other process modes start their
own processes against it. Every process runs in its own process group and is
killed in teardown.

Postgres and an external Prefect server run in Docker containers
(testcontainers), shared by the session. Tests that need them are skipped
when Docker is not available. ``HP_POSTGRES_IMAGE``, ``HP_PREFECT_IMAGE`` and
``HP_AZURITE_IMAGE`` override the images (for example to use a registry mirror).
"""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
from sqlalchemy import create_engine, make_url, text

if TYPE_CHECKING:
    from testcontainers.community.postgres import PostgresContainer
    from testcontainers.core.container import DockerContainer

HP = str(Path(sys.executable).parent / "heliopuddle")
PROJECT = "itest"
STARTUP_TIMEOUT_S = 180.0
STOP_TIMEOUT_S = 40.0


@pytest.fixture(autouse=True)
def _proxy_user_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """Act as the trusted proxy: send a user header on every httpx request.

    The servers here run in ``header`` auth mode, which refuses a request
    without the user header (AUTH-20). Requests that set headers themselves
    keep them; other auth modes ignore the header.
    """
    for cls in (httpx.Client, httpx.AsyncClient):
        original = cls.__init__

        def init(self, *args, __original=original, **kwargs):  # type: ignore[no-untyped-def]
            headers = httpx.Headers(kwargs.get("headers"))
            headers.setdefault("X-Forwarded-User", "itest")
            kwargs["headers"] = headers
            __original(self, *args, **kwargs)

        monkeypatch.setattr(cls, "__init__", init)


POSTGRES_IMAGE = os.environ.get("HP_POSTGRES_IMAGE", "postgres:16-alpine")
PREFECT_IMAGE = os.environ.get("HP_PREFECT_IMAGE", "prefecthq/prefect:3.9-python3.12")
PREFECT_CONTAINER_PORT = 4200
AZURITE_IMAGE = os.environ.get("HP_AZURITE_IMAGE", "mcr.microsoft.com/azure-storage/azurite")


def free_port() -> int:
    """A TCP port that is free on 127.0.0.1 now."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def base_env(**extra: str) -> dict[str, str]:
    """Our environment without ``HELIOPUDDLE_*``/``PREFECT_API_URL``, plus ``extra``.

    These suites test the storage, indexing and rules behaviour, not auth, so
    servers run in ``header`` auth mode (the product default is ``local``);
    pass ``HELIOPUDDLE_AUTH_MODE`` in ``extra`` to override.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("HELIOPUDDLE_")
        and k not in {"PREFECT_API_URL", "PREFECT_SERVER_API_PORT", "PREFECT_HOME"}
    }
    env["HELIOPUDDLE_AUTH_MODE"] = "header"
    env.update(extra)
    return env


@dataclass
class HpProcess:
    """A running ``heliopuddle`` process."""

    proc: subprocess.Popen[bytes]
    log_path: Path
    env: dict[str, str] = field(repr=False)

    def log(self) -> str:
        """The process's combined output so far."""
        try:
            return self.log_path.read_text(errors="replace")
        except OSError:
            return ""

    def signal(self, sig: int) -> None:
        """Send ``sig`` to the process group."""
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.proc.pid, sig)

    def stop(self, timeout: float = STOP_TIMEOUT_S) -> int:
        """SIGTERM the process group; SIGKILL it after ``timeout``. Returns the exit code."""
        if self.proc.poll() is None:
            self.signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout)
            except subprocess.TimeoutExpired:
                self.signal(signal.SIGKILL)
                self.proc.wait(10)
        self.signal(signal.SIGKILL)  # anything left in the group (flow runs)
        return self.proc.returncode

    def wait_for(self, url: str, timeout: float = STARTUP_TIMEOUT_S) -> httpx.Response:
        """Poll ``url`` until it answers 200; fail with the log if the process exits."""
        deadline = time.monotonic() + timeout
        last = "no answer"
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                pytest.fail(f"heliopuddle exited with {self.proc.returncode}:\n{self.log()}")
            try:
                r = httpx.get(url, timeout=5, follow_redirects=True)
                if r.status_code == 200:
                    return r
                last = f"{r.status_code} {r.text[:200]}"
            except httpx.HTTPError as e:
                last = repr(e)
            time.sleep(0.5)
        pytest.fail(f"{url} not 200 after {timeout:.0f}s (last: {last}):\n{self.log()}")


def start_hp(args: list[str], env: dict[str, str], log_path: Path) -> HpProcess:
    """Start ``heliopuddle *args`` in its own process group."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("wb") as log:
        proc = subprocess.Popen(
            [HP, *args],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return HpProcess(proc=proc, log_path=log_path, env=env)


@pytest.fixture
def run_hp(tmp_path: Path) -> Iterator[list[HpProcess]]:
    """Start extra processes in a test; all are stopped in teardown."""
    started: list[HpProcess] = []
    yield started
    for p in started:
        p.stop()


@dataclass
class Deployment:
    """The shared server: its URLs, store and environment."""

    process: HpProcess
    base_url: str
    prefect_url: str
    store: Path
    config_dir: Path
    data_dir: Path
    logs: Path
    database_url: str | None = None

    @property
    def api_url(self) -> str:
        """Base URL of the API (``.../api/v1``)."""
        return f"{self.base_url}/api/v1"

    @property
    def db_path(self) -> Path:
        """The SQLite database file (when ``database_url`` is not set)."""
        return self.data_dir / "heliopuddle.db"

    def shared_env(self, **extra: str) -> dict[str, str]:
        """Env for another process using the same config, data, database and Prefect."""
        values = {
            "HELIOPUDDLE_CONFIG_DIR": str(self.config_dir),
            "HELIOPUDDLE_DATA_DIR": str(self.data_dir),
            "PREFECT_API_URL": self.prefect_url,
            "PREFECT_HOME": str(self.data_dir / "client-prefect"),
        }
        if self.database_url is not None:
            values["HELIOPUDDLE_DATABASE_URL"] = self.database_url
        return base_env(**{**values, **extra})


def make_project_dirs(root: Path) -> tuple[Path, Path, Path, Path]:
    """Create ``store``, ``config``, ``data`` and ``logs`` under ``root``.

    The config dir holds the project ``itest`` with one ``file://`` storage
    location, ``primary``, at the store.
    """
    store, config_dir, data_dir, logs = (root / n for n in ("store", "config", "data", "logs"))
    for d in (store, config_dir, data_dir, logs):
        d.mkdir()
    (config_dir / f"{PROJECT}.yaml").write_text(
        f"project: {PROJECT}\n"
        "storage_locations:\n"
        "  - name: primary\n"
        f"    url: file://{store}\n"
        "    storage_options:\n"
        "      auto_mkdir: true\n"
    )
    return store, config_dir, data_dir, logs


@pytest.fixture(scope="session")
def hp_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Deployment]:
    """The full server: embedded Prefect, API and UI on one port, served deployments."""
    root = tmp_path_factory.mktemp("hp")
    store, config_dir, data_dir, logs = make_project_dirs(root)
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


# --- Docker: Postgres and an external Prefect server ----------------------------------


def require_docker() -> None:
    """Skip the calling test when no Docker daemon answers."""
    try:
        import docker

        docker.from_env().ping()
    except Exception as e:  # no client library, no socket, daemon down
        pytest.skip(f"Docker is not available: {type(e).__name__}: {e}")


def _host_ip(container: DockerContainer) -> str:
    host = container.get_container_host_ip()
    return "127.0.0.1" if host in {"localhost", "0.0.0.0"} else host


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer]:
    """A Postgres server for the session (``HP_POSTGRES_IMAGE``)."""
    require_docker()
    from testcontainers.community.postgres import PostgresContainer

    container = PostgresContainer(
        POSTGRES_IMAGE, username="hp", password="hp-secret", dbname="postgres", driver="psycopg"
    )
    container.start()
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="session")
def postgres_database(postgres_container: PostgresContainer) -> Iterator[Callable[[], str]]:
    """A factory: each call creates an empty database and returns its URL.

    The URLs use ``postgresql+psycopg://`` and ``127.0.0.1``. The databases
    are dropped with the container.
    """
    admin_url = make_url(postgres_container.get_connection_url()).set(
        host=_host_ip(postgres_container)
    )
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")

    def create() -> str:
        name = f"hp_{uuid.uuid4().hex[:12]}"
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
        return admin_url.set(database=name).render_as_string(hide_password=False)

    yield create
    admin.dispose()


@pytest.fixture
def postgres_url(postgres_database: Callable[[], str]) -> str:
    """URL of a fresh, empty Postgres database for one test."""
    return postgres_database()


@pytest.fixture(scope="session")
def external_prefect() -> Iterator[str]:
    """A Prefect API server in a container (``HP_PREFECT_IMAGE``); yields its API URL.

    The container is only the API server: hp serves the deployments itself
    and runs flow runs in its own child processes, so the container never
    needs to reach hp.
    """
    require_docker()
    from testcontainers.core.container import DockerContainer

    container = (
        DockerContainer(PREFECT_IMAGE)
        .with_command(f"prefect server start --host 0.0.0.0 --port {PREFECT_CONTAINER_PORT}")
        .with_env("PREFECT_SERVER_ANALYTICS_ENABLED", "false")
        .with_env("PREFECT_UI_ENABLED", "false")
        .with_exposed_ports(PREFECT_CONTAINER_PORT)
    )
    container.start()
    try:
        port = container.get_exposed_port(PREFECT_CONTAINER_PORT)
        api_url = f"http://{_host_ip(container)}:{port}/api"
        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        while True:
            with contextlib.suppress(httpx.HTTPError):
                if httpx.get(f"{api_url}/health", timeout=5).status_code == 200:
                    break
            if time.monotonic() > deadline:
                logs = b"\n".join(x for x in container.get_logs() if x).decode(errors="replace")
                pytest.fail(f"Prefect container not healthy at {api_url}:\n{logs[-5000:]}")
            time.sleep(1)
        yield api_url
    finally:
        container.stop()


# --- Docker: Azurite (Azure Blob emulator) ------------------------------------------


AZURITE_ACCOUNT = "devstoreaccount1"
# Azurite's well-known development key (public, documented by Microsoft).
AZURITE_KEY = (
    "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="
)
AZURITE_BLOB_PORT = 10000


def azurite_connection_string(host: str, port: int) -> str:
    """The Azurite development connection string for a blob endpoint at ``host:port``."""
    return (
        f"DefaultEndpointsProtocol=http;AccountName={AZURITE_ACCOUNT};"
        f"AccountKey={AZURITE_KEY};"
        f"BlobEndpoint=http://{host}:{port}/{AZURITE_ACCOUNT};"
    )


@pytest.fixture(scope="session")
def azurite() -> Iterator[str]:
    """An Azurite blob service for the session (``HP_AZURITE_IMAGE``); yields its connection string.

    The connection string points at the host-mapped port on ``127.0.0.1``.
    """
    require_docker()
    from testcontainers.core.container import DockerContainer

    container = (
        DockerContainer(AZURITE_IMAGE)
        .with_command(
            "azurite-blob --blobHost 0.0.0.0 --blobPort 10000 --skipApiVersionCheck --loose"
        )
        .with_exposed_ports(AZURITE_BLOB_PORT)
    )
    container.start()
    try:
        port = int(container.get_exposed_port(AZURITE_BLOB_PORT))
        conn = azurite_connection_string(_host_ip(container), port)
        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        while True:
            try:
                from azure.storage.blob import BlobServiceClient

                BlobServiceClient.from_connection_string(conn).get_service_properties()
                break
            except Exception as e:
                if time.monotonic() > deadline:
                    pytest.fail(f"Azurite not ready on port {port}: {e!r}")
                time.sleep(0.5)
        yield conn
    finally:
        container.stop()


@pytest.fixture(scope="module")
def hp_postgres_server(
    tmp_path_factory: pytest.TempPathFactory,
    postgres_database: Callable[[], str],
    external_prefect: str,
) -> Iterator[Deployment]:
    """``server start`` on Postgres with an external Prefect, configured by env vars only.

    No command-line flags: the database, Prefect API, config and data dirs
    and the port all come from the environment (DEP-04).
    """
    root = tmp_path_factory.mktemp("hp-pg")
    store, config_dir, data_dir, logs = make_project_dirs(root)
    database_url = postgres_database()
    port = free_port()
    env = base_env(
        HELIOPUDDLE_DATABASE_URL=database_url,
        HELIOPUDDLE_CONFIG_DIR=str(config_dir),
        HELIOPUDDLE_DATA_DIR=str(data_dir),
        HELIOPUDDLE_PORT=str(port),
        PREFECT_API_URL=external_prefect,
        PREFECT_HOME=str(data_dir / "prefect-client"),
    )
    process = start_hp(["server", "start"], env, logs / "server.log")
    try:
        deployment = Deployment(
            process=process,
            base_url=f"http://127.0.0.1:{port}",
            prefect_url=external_prefect,
            store=store,
            config_dir=config_dir,
            data_dir=data_dir,
            logs=logs,
            database_url=database_url,
        )
        process.wait_for(f"{deployment.api_url}/health")
        yield deployment
    finally:
        process.stop()

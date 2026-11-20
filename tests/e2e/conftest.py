"""End-to-end fixtures: build (or reuse) the Docker image and run one container.

The container gets:

- a host temp dir mounted at ``/data`` *and* at its own absolute host path, so a
  project URL of ``file://<host path>`` means the same directory to the server
  (inside the container) and to the client (on the host, writing directly to
  the store);
- a host temp dir with one project YAML mounted at ``/config``;
- a fresh named volume at ``/var/lib/heliopuddle``;
- ``HELIOPUDDLE_AUTH_MODE=header``: the image defaults to ``local`` auth, and the
  suites that drive it as a header-trusting API (``user="e2e"``) keep working.
  ``hp_local_auth`` runs a container in ``local`` mode for the login test.

``hp_azure`` (DEP-03) runs a second container whose storage location is an
``az://`` URL on an Azurite container instead of a volume.

Environment:

- ``HP_E2E_IMAGE``: use this image instead of building ``heliopuddle:e2e``.
- ``HP_E2E_BUILD_ARGS``: extra ``docker build`` arguments (for example
  ``--secret id=ca,src=/path/to/ca-bundle.crt`` behind a TLS-intercepting proxy).
- ``HP_AZURITE_IMAGE``: the Azurite image (default
  ``mcr.microsoft.com/azure-storage/azurite``).
- ``HP_CHROMIUM``: path to a Chromium executable for Playwright (else Playwright
  uses its own install).

The whole suite is skipped when Docker is not available.
"""

from __future__ import annotations

import os
import shlex
import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Browser, sync_playwright

from heliopuddle.client import HelioPuddleClient

from .harness import REPORTS, ROOT, HpContainer, docker, docker_logs

PROJECT = "e2e"
HEALTH_TIMEOUT_S = 90.0


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip every e2e test (rather than error) when Docker is unavailable."""
    e2e_dir = Path(__file__).parent
    e2e_items = [i for i in items if e2e_dir in i.path.parents]
    if e2e_items and not _docker_available():
        skip = pytest.mark.skip(reason="Docker is not available")
        for item in e2e_items:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def hp_image() -> str:
    """The image under test: ``HP_E2E_IMAGE``, or ``heliopuddle:e2e`` built from this tree."""
    image = os.environ.get("HP_E2E_IMAGE")
    if image:
        return image
    image = "heliopuddle:e2e"
    extra = shlex.split(os.environ.get("HP_E2E_BUILD_ARGS", ""))
    docker(
        "build", "-f", str(ROOT / "docker/Dockerfile"), "-t", image, *extra, str(ROOT),
        timeout=1800,
    )  # fmt: skip
    return image


@pytest.fixture(scope="session")
def hp_container(
    hp_image: str, tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest
) -> Iterator[HpContainer]:
    """Run the image with temp ``/data`` and ``/config`` dirs; wait for ``/api/v1/health``."""
    data_dir = tmp_path_factory.mktemp("data").resolve()
    archive_dir = tmp_path_factory.mktemp("archive").resolve()
    config_dir = tmp_path_factory.mktemp("config").resolve()
    # The container runs as a non-root user with a different uid.
    data_dir.chmod(0o777)
    archive_dir.chmod(0o777)
    config_dir.chmod(0o755)
    (config_dir / f"{PROJECT}.yaml").write_text(
        f"project: {PROJECT}\n"
        "storage_locations:\n"
        "  - name: primary\n"
        f"    url: file://{data_dir}\n"
        "    accept_external_changes: true\n"
        "  - name: archive\n"
        f"    url: file://{archive_dir}\n"
        "    quota: 1GB\n"
        "rules:\n"
        "  - name: archive-ui\n"
        "    action: copy\n"
        "    source: primary\n"
        "    destination: archive\n"
        "    match:\n"
        "      paths: ['e2e/rules/**']\n"
        "    options:\n"
        "      check_for_space_overhead: 0B\n"
        "      retrigger: false\n"
        "    schedule: '0 0 1 1 *'\n"
    )
    (config_dir / f"{PROJECT}.yaml").chmod(0o644)

    suffix = uuid.uuid4().hex[:8]
    name = f"hp-e2e-{suffix}"
    volume = f"hp-e2e-state-{suffix}"
    prefect_port = _free_host_port()
    docker("volume", "create", volume)
    container = docker(
        "run", "-d", "--name", name,
        "-p", "127.0.0.1::8080",
        "-p", f"127.0.0.1:{prefect_port}:4200",
        "-e", f"HELIOPUDDLE_PREFECT_UI_URL=http://127.0.0.1:{prefect_port}",
        "-e", "HELIOPUDDLE_AUTH_MODE=header",
        "-v", f"{data_dir}:/data",
        "-v", f"{data_dir}:{data_dir}",
        "-v", f"{archive_dir}:{archive_dir}",
        "-v", f"{config_dir}:/config:ro",
        "-v", f"{volume}:/var/lib/heliopuddle",
        hp_image,
    )  # fmt: skip
    try:
        host_port = docker("port", container, "8080/tcp").splitlines()[0].rsplit(":", 1)[1]
        hp = HpContainer(
            id=container,
            base_url=f"http://127.0.0.1:{host_port}",
            data_dir=data_dir,
            project=PROJECT,
        )
        _wait_healthy(hp)
        _wait_adoption(hp, f"http://127.0.0.1:{prefect_port}/api")
        yield hp
    finally:
        logs = docker_logs(container)
        REPORTS.mkdir(parents=True, exist_ok=True)
        (REPORTS / "e2e-container.log").write_text(logs)
        if request.session.testsfailed:
            print(f"\n----- docker logs {name} -----\n{logs}\n----- end docker logs -----")
        # Files the container wrote belong to its uid; let the host clean them up.
        docker("exec", container, "chmod", "-R", "a+rwX", str(data_dir), check=False)
        docker("exec", container, "chmod", "-R", "a+rwX", str(archive_dir), check=False)
        docker("rm", "-f", "-v", container, check=False)
        docker("volume", "rm", "-f", volume, check=False)


def _wait_healthy(hp: HpContainer) -> None:
    deadline = time.monotonic() + HEALTH_TIMEOUT_S
    last = "no response"
    while time.monotonic() < deadline:
        if docker("inspect", "-f", "{{.State.Running}}", hp.id) != "true":
            pytest.fail(f"container exited before becoming healthy:\n{docker_logs(hp.id)}")
        try:
            r = httpx.get(f"{hp.api_url}/health", timeout=5)
            if r.status_code == 200:
                return
            last = f"{r.status_code} {r.text}"
        except httpx.HTTPError as exc:
            last = repr(exc)
        time.sleep(1)
    logs = docker_logs(hp.id)
    pytest.fail(f"/api/v1/health not 200 after {HEALTH_TIMEOUT_S:.0f}s (last: {last}):\n{logs}")


def _wait_adoption(hp: HpContainer, prefect_url: str) -> None:
    deadline = time.monotonic() + HEALTH_TIMEOUT_S
    with httpx.Client(base_url=prefect_url, timeout=30) as client:
        while time.monotonic() < deadline:
            response = client.post(
                "/flow_runs/filter",
                json={"flow_runs": {"tags": {"all_": ["heliopuddle-adoption"]}}},
            )
            response.raise_for_status()
            runs = [
                run for run in response.json() if run["parameters"].get("project") == hp.project
            ]
            for run in runs:
                assert run["state"]["type"] not in {"FAILED", "CRASHED", "CANCELLED"}, run["state"]
            if len(runs) == 2 and all(run["state"]["type"] == "COMPLETED" for run in runs):
                return
            time.sleep(0.5)
    pytest.fail(f"startup adoption did not complete:\n{docker_logs(hp.id)}")


# --- DEP-03: the image with an Azurite storage location ------------------------------

AZURE_PROJECT = "e2eaz"
AZURE_CONTAINER = "hpe2e"
AZURE_PREFIX = "store"
AZURITE_IMAGE = os.environ.get("HP_AZURITE_IMAGE", "mcr.microsoft.com/azure-storage/azurite")
AZURITE_ACCOUNT = "devstoreaccount1"
# Azurite's well-known development key (public, documented by Microsoft).
AZURITE_KEY = (
    "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="
)
AZURE_EXISTING = {
    "obs/2026/obs_v001.csv": b"t,v\n1,2\n",
    "obs/2026/obs_v002.csv": b"t,v\n1,3\n",
    "docs/readme.txt": b"adopted from the container\n",
}


@dataclass(frozen=True)
class AzureHp:
    """The image running against Azurite.

    Attributes:
        hp: the heliopuddle container (its network is Azurite's).
        azurite: the Azurite container id.
        connection_string: works on the host and inside the hp container.
        prefect_url: the embedded Prefect API, from the host.
    """

    hp: HpContainer
    azurite: str
    connection_string: str
    prefect_url: str

    def blob_fs(self) -> Any:
        """An adlfs filesystem on Azurite, from the host."""
        from adlfs import AzureBlobFileSystem

        return AzureBlobFileSystem(connection_string=self.connection_string)

    def blob_path(self, rel: str) -> str:
        """The adlfs path of a file of the project's storage location."""
        return f"{AZURE_CONTAINER}/{AZURE_PREFIX}/{rel}"


def _free_host_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def hp_azure(
    hp_image: str, tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest
) -> Iterator[AzureHp]:
    """Run Azurite, upload some blobs, then run the image with an ``az://`` storage location.

    The hp container joins Azurite's network namespace
    (``--network container:<azurite>``) and Azurite listens on the same port
    number inside and on the host (``-p 127.0.0.1:P:P --blobPort P``). So
    ``http://127.0.0.1:P`` reaches Azurite both from the server and from the
    client on the host, and the connection string the API hands out works for
    both. hp's own ports are published on the Azurite container.
    """
    suffix = uuid.uuid4().hex[:8]
    blob_port = _free_host_port()
    conn = (
        f"DefaultEndpointsProtocol=http;AccountName={AZURITE_ACCOUNT};"
        f"AccountKey={AZURITE_KEY};"
        f"BlobEndpoint=http://127.0.0.1:{blob_port}/{AZURITE_ACCOUNT};"
    )
    azurite = docker(
        "run", "-d", "--name", f"hp-e2e-azurite-{suffix}",
        "-p", f"127.0.0.1:{blob_port}:{blob_port}",
        "-p", "127.0.0.1::8080",
        "-p", "127.0.0.1::4200",
        AZURITE_IMAGE,
        "azurite-blob", "--blobHost", "0.0.0.0", "--blobPort", str(blob_port),
        "--skipApiVersionCheck", "--loose",
    )  # fmt: skip
    hp_id: str | None = None
    volume = f"hp-e2e-az-state-{suffix}"
    try:
        from adlfs import AzureBlobFileSystem

        fs = AzureBlobFileSystem(connection_string=conn)
        deadline = time.monotonic() + HEALTH_TIMEOUT_S
        while True:
            try:
                fs.mkdir(AZURE_CONTAINER)
                break
            except Exception:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.5)
        for rel, data in AZURE_EXISTING.items():
            fs.pipe(f"{AZURE_CONTAINER}/{AZURE_PREFIX}/{rel}", data)

        config_dir = tmp_path_factory.mktemp("az-config").resolve()
        config_dir.chmod(0o755)
        (config_dir / f"{AZURE_PROJECT}.yaml").write_text(
            f"project: {AZURE_PROJECT}\n"
            "storage_locations:\n"
            "  - name: blob\n"
            f"    url: az://{AZURE_CONTAINER}/{AZURE_PREFIX}\n"
            "    create_root: true\n"
            "    storage_options:\n"
            "      connection_string: ${AZURE_CONN}\n"
            "index:\n"
            "  settle_time: 0s\n"
        )
        (config_dir / f"{AZURE_PROJECT}.yaml").chmod(0o644)
        docker("volume", "create", volume)
        hp_id = docker(
            "run", "-d", "--name", f"hp-e2e-az-{suffix}",
            "--network", f"container:{azurite}",
            "-e", f"AZURE_CONN={conn}",
            "-e", "HELIOPUDDLE_AUTH_MODE=header",
            "-v", f"{config_dir}:/config:ro",
            "-v", f"{volume}:/var/lib/heliopuddle",
            hp_image,
        )  # fmt: skip
        api_port = docker("port", azurite, "8080/tcp").splitlines()[0].rsplit(":", 1)[1]
        prefect_port = docker("port", azurite, "4200/tcp").splitlines()[0].rsplit(":", 1)[1]
        hp = HpContainer(
            id=hp_id,
            base_url=f"http://127.0.0.1:{api_port}",
            data_dir=config_dir,  # no /data volume: the store is Azurite
            project=AZURE_PROJECT,
        )
        _wait_healthy(hp)
        yield AzureHp(
            hp=hp,
            azurite=azurite,
            connection_string=conn,
            prefect_url=f"http://127.0.0.1:{prefect_port}/api",
        )
    finally:
        if hp_id is not None:
            logs = docker_logs(hp_id)
            REPORTS.mkdir(parents=True, exist_ok=True)
            (REPORTS / "e2e-azure-container.log").write_text(logs)
            if request.session.testsfailed:
                print(f"\n----- docker logs (azure) -----\n{logs}\n----- end docker logs -----")
            docker("rm", "-f", "-v", hp_id, check=False)
        docker("rm", "-f", "-v", azurite, check=False)
        docker("volume", "rm", "-f", volume, check=False)


LOCAL_AUTH_PROJECT = "e2elogin"
LOCAL_ADMIN_PASSWORD = "e2e-admin-password-1"


@dataclass(frozen=True)
class LocalAuthHp:
    """The image in ``local`` auth mode.

    Attributes:
        hp: the container (its project has one storage location under ``hp.data_dir``).
        admin_password: the first admin's password (user ``admin``).
    """

    hp: HpContainer
    admin_password: str

    def client(self) -> HelioPuddleClient:
        """A client on the host logged in as ``admin``."""
        return HelioPuddleClient(
            self.hp.api_url, self.hp.project, username="admin", password=self.admin_password
        )


@pytest.fixture(scope="session")
def hp_local_auth(
    hp_image: str, tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest
) -> Iterator[LocalAuthHp]:
    """Run the image in its default ``local`` auth mode with a known admin password (AUTH-16)."""
    data_dir = tmp_path_factory.mktemp("login-data").resolve()
    config_dir = tmp_path_factory.mktemp("login-config").resolve()
    data_dir.chmod(0o777)
    config_dir.chmod(0o755)
    (config_dir / f"{LOCAL_AUTH_PROJECT}.yaml").write_text(
        f"project: {LOCAL_AUTH_PROJECT}\n"
        "storage_locations:\n"
        "  - name: primary\n"
        f"    url: file://{data_dir}\n"
        "    accept_external_changes: true\n"
    )
    (config_dir / f"{LOCAL_AUTH_PROJECT}.yaml").chmod(0o644)
    suffix = uuid.uuid4().hex[:8]
    volume = f"hp-e2e-login-state-{suffix}"
    docker("volume", "create", volume)
    container = docker(
        "run", "-d", "--name", f"hp-e2e-login-{suffix}",
        "-p", "127.0.0.1::8080",
        "-e", "HELIOPUDDLE_AUTH_MODE=local",
        "-e", f"HELIOPUDDLE_ADMIN_PASSWORD={LOCAL_ADMIN_PASSWORD}",
        "-v", f"{data_dir}:{data_dir}",
        "-v", f"{config_dir}:/config:ro",
        "-v", f"{volume}:/var/lib/heliopuddle",
        hp_image,
    )  # fmt: skip
    try:
        host_port = docker("port", container, "8080/tcp").splitlines()[0].rsplit(":", 1)[1]
        hp = HpContainer(
            id=container,
            base_url=f"http://127.0.0.1:{host_port}",
            data_dir=data_dir,
            project=LOCAL_AUTH_PROJECT,
        )
        _wait_healthy(hp)
        yield LocalAuthHp(hp, LOCAL_ADMIN_PASSWORD)
    finally:
        logs = docker_logs(container)
        REPORTS.mkdir(parents=True, exist_ok=True)
        (REPORTS / "e2e-login-container.log").write_text(logs)
        if request.session.testsfailed:
            print(f"\n----- docker logs (login) -----\n{logs}\n----- end docker logs -----")
        docker("exec", container, "chmod", "-R", "a+rwX", str(data_dir), check=False)
        docker("rm", "-f", "-v", container, check=False)
        docker("volume", "rm", "-f", volume, check=False)


@pytest.fixture
def hp_client(hp_container: HpContainer) -> Iterator[HelioPuddleClient]:
    """A client on the host, talking to the container's API."""
    with HelioPuddleClient(hp_container.api_url, project=hp_container.project, user="e2e") as c:
        yield c


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
            headers.setdefault("X-Forwarded-User", "e2e")
            kwargs["headers"] = headers
            __original(self, *args, **kwargs)

        monkeypatch.setattr(cls, "__init__", init)


@pytest.fixture(scope="session")
def browser() -> Iterator[Browser]:
    """A headless Chromium (``HP_CHROMIUM`` if set, else Playwright's own install)."""
    executable = os.environ.get("HP_CHROMIUM") or None
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True, executable_path=executable)
        # Act as the trusted proxy in front of a ``header``-mode container: every
        # page sends the user header (AUTH-20); ``local`` mode ignores it.
        for name in ("new_page", "new_context"):
            original = getattr(b, name)

            def opener(*args, __original=original, **kwargs):  # type: ignore[no-untyped-def]
                kwargs.setdefault("extra_http_headers", {"X-Forwarded-User": "e2e"})
                return __original(*args, **kwargs)

            setattr(b, name, opener)
        try:
            yield b
        finally:
            b.close()

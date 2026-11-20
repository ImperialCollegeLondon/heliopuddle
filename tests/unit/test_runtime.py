"""The Prefect runtime (embedded or external), served deployments and concurrency (SVC-01)."""

from __future__ import annotations

import contextvars
import os
import socket
import subprocess
import sys
import types
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call

import httpx
import pytest

from heliopuddle.server.services import runtime
from heliopuddle.server.services.concurrency import (
    hp_concurrency,
    rule_limit,
    storage_location_limit,
)
from heliopuddle.server.services.deploy import (
    DELETE_FILES_DEPLOYMENT,
    DEPLOYMENTS,
    VERIFY_FILES_DEPLOYMENT,
    DeploymentSpec,
    ServicesRunner,
    build_deployments,
)
from heliopuddle.server.services.runtime import (
    EmbeddedPrefectServer,
    PrefectStartupError,
    check_api,
    free_port,
    port_is_free,
    start_prefect,
    use_api_url,
)
from heliopuddle.settings import Settings

# A stand-in for `prefect server start`: answers GET /api/health on its port.
FAKE_SERVER = """
import http.server, os
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == "/api/health" else 404)
        self.end_headers()
        self.wfile.write(b"true")
    def log_message(self, *a):
        pass
print("fake prefect home", os.environ["PREFECT_HOME"], flush=True)
port = int(os.environ["PREFECT_SERVER_API_PORT"])
http.server.HTTPServer((os.environ["PREFECT_SERVER_API_HOST"], port), H).serve_forever()
"""


@pytest.fixture
def restore_prefect_env() -> Iterator[None]:
    """Restore ``PREFECT_API_URL`` and Prefect's global settings (the harness replaces them)."""
    import prefect.context

    names = ("PREFECT_API_URL", "PREFECT_LOGGING_TO_API_WHEN_MISSING_FLOW")
    before = {n: os.environ.get(n) for n in names}
    global_ctx = prefect.context.GLOBAL_SETTINGS_CONTEXT
    yield
    for name, value in before.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    prefect.context.GLOBAL_SETTINGS_CONTEXT = global_ctx


def in_fresh_context(fn: Any) -> Any:
    """Run ``fn`` outside any active Prefect settings context (as a new thread would)."""
    return contextvars.Context().run(fn)


def _fake(server: EmbeddedPrefectServer, script: str) -> EmbeddedPrefectServer:
    server.command = lambda: [sys.executable, "-c", script]  # type: ignore[method-assign]
    return server


# --- external server -------------------------------------------------------------


@pytest.mark.req("SVC-01")
def test_check_api_unreachable_is_clear() -> None:
    url = f"http://127.0.0.1:{free_port()}/api"
    with pytest.raises(PrefectStartupError, match=r"unreachable.*") as exc:
        check_api(url, timeout=2)
    assert url in str(exc.value)
    assert "PREFECT_API_URL" in str(exc.value)


@pytest.mark.req("SVC-01")
def test_check_api_unhealthy_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime.httpx, "get", lambda *a, **k: httpx.Response(503))
    with pytest.raises(PrefectStartupError, match="returned 503"):
        check_api("http://prefect/api")


@pytest.mark.req("SVC-01")
@pytest.mark.usefixtures("restore_prefect_env")
def test_external_api_is_checked_and_used(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    os.environ.pop("PREFECT_LOGGING_TO_API_WHEN_MISSING_FLOW", None)
    checked: list[str] = []
    monkeypatch.setattr(runtime, "configured_api_url", lambda: "http://prefect:4200/api")
    monkeypatch.setattr(runtime, "check_api", lambda url: checked.append(url))

    rt = start_prefect(Settings(data_dir=tmp_path))

    assert checked == ["http://prefect:4200/api"]
    assert rt.api_url == "http://prefect:4200/api"
    assert rt.embedded is None
    # inline task runs (outside a flow) do not warn about logs they cannot send
    assert os.environ["PREFECT_LOGGING_TO_API_WHEN_MISSING_FLOW"] == "ignore"
    rt.stop()


@pytest.mark.req("SVC-01")
@pytest.mark.usefixtures("restore_prefect_env")
def test_external_api_unreachable_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    url = f"http://127.0.0.1:{free_port()}/api"
    monkeypatch.setattr(runtime, "configured_api_url", lambda: url)
    with pytest.raises(PrefectStartupError, match="unreachable"):
        start_prefect(Settings(data_dir=tmp_path))


def test_configured_api_url_reads_prefect_settings(restore_prefect_env: None) -> None:
    use_api_url("http://configured:1/api")
    assert in_fresh_context(runtime.configured_api_url) == "http://configured:1/api"


# --- embedded server ---------------------------------------------------------------


def test_embedded_server_from_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = Settings(data_dir=tmp_path / "d")
    env = {"PREFECT_SERVER_API_HOST": "0.0.0.0", "PREFECT_SERVER_API_PORT": "4999"}
    server = EmbeddedPrefectServer.from_settings(s, env)
    assert server.home == (tmp_path / "d" / "prefect").resolve()
    assert (server.host, server.port) == ("0.0.0.0", 4999)
    assert server.api_url == "http://127.0.0.1:4999/api"

    monkeypatch.setattr(runtime, "port_is_free", lambda host, port: True)
    assert EmbeddedPrefectServer.from_settings(s, {}).port == 4200
    assert EmbeddedPrefectServer.from_settings(s, {}).host == "127.0.0.1"

    monkeypatch.setattr(runtime, "port_is_free", lambda host, port: False)
    assert EmbeddedPrefectServer.from_settings(s, {}).port != 4200


def test_embedded_command_and_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PREFECT_API_URL", "http://should-not-leak/api")
    server = EmbeddedPrefectServer(home=tmp_path, host="127.0.0.1", port=4321)
    cmd = server.command()
    assert cmd[:3] == [sys.executable, "-m", "heliopuddle.server.services._prefect_child"]
    assert cmd[3:] == ["server", "start", "--host", "127.0.0.1", "--port", "4321"]
    env = server.child_env()
    assert "PREFECT_API_URL" not in env
    assert env["PREFECT_HOME"] == str(tmp_path)
    assert env["PREFECT_SERVER_API_PORT"] == "4321"


@pytest.mark.req("SVC-01")
def test_embedded_server_starts_and_stops(tmp_path: Path) -> None:
    server = _fake(EmbeddedPrefectServer(home=tmp_path / "prefect", port=free_port()), FAKE_SERVER)

    url = server.start()

    assert url == server.api_url
    assert httpx.get(f"{url}/health").status_code == 200
    process = server.process
    assert process is not None
    server.stop()
    assert process.poll() is not None
    assert server.process is None
    assert "fake prefect home" in server.log_tail()
    server.stop()  # idempotent


@pytest.mark.req("SVC-01")
def test_embedded_server_exiting_early_reports_its_log(tmp_path: Path) -> None:
    script = "import sys; print('database is locked'); sys.exit(3)"
    server = _fake(EmbeddedPrefectServer(home=tmp_path, port=free_port()), script)

    with pytest.raises(PrefectStartupError, match="exited with code 3") as exc:
        server.start()
    assert "database is locked" in str(exc.value)


@pytest.mark.req("SVC-01")
def test_embedded_server_health_timeout(tmp_path: Path) -> None:
    script = "import time; time.sleep(60)"
    server = _fake(EmbeddedPrefectServer(home=tmp_path, port=free_port()), script)
    server.startup_timeout = 1.0

    with pytest.raises(PrefectStartupError, match="did not answer"):
        server.start()
    assert server.process is None


def test_embedded_server_killed_when_sigterm_is_ignored(tmp_path: Path) -> None:
    script = "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n" + FAKE_SERVER
    server = _fake(EmbeddedPrefectServer(home=tmp_path, port=free_port()), script)
    server.start()
    process = server.process
    assert process is not None

    server.stop(timeout=1.0)

    assert process.poll() is not None


@pytest.mark.req("SVC-01")
@pytest.mark.parametrize("times_out", [False, True])
def test_embedded_server_stops_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, times_out: bool
) -> None:
    process = MagicMock(spec=subprocess.Popen)
    process.poll.return_value = None
    if times_out:
        process.wait.side_effect = [subprocess.TimeoutExpired("prefect", 1), 0]
    server = EmbeddedPrefectServer(home=tmp_path)
    server.process = process

    with monkeypatch.context() as patch:
        patch.setattr(sys, "platform", "win32")
        patch.delattr(os, "killpg", raising=False)
        patch.delattr(runtime.signal, "SIGKILL", raising=False)
        server.stop(timeout=1)

    process.terminate.assert_called_once_with()
    if times_out:
        process.kill.assert_called_once_with()
        assert process.wait.call_args_list == [call(timeout=1), call(timeout=1)]
    else:
        process.kill.assert_not_called()
        process.wait.assert_called_once_with(timeout=1)
    assert server.process is None


@pytest.mark.req("SVC-01")
def test_embedded_server_port_in_use(tmp_path: Path) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        assert not port_is_free("127.0.0.1", port)
        server = EmbeddedPrefectServer(home=tmp_path, port=port)
        with pytest.raises(PrefectStartupError, match="in use"):
            server.start()


@pytest.mark.req("SVC-01")
def test_start_prefect_embedded_sets_and_restores_api_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restore_prefect_env: None
) -> None:
    from prefect.settings import PREFECT_API_URL

    use_api_url(None)
    monkeypatch.setenv("PREFECT_SERVER_API_PORT", str(free_port()))
    monkeypatch.setattr(
        EmbeddedPrefectServer,
        "command",
        lambda self: [sys.executable, "-c", FAKE_SERVER],
    )

    rt = in_fresh_context(lambda: start_prefect(Settings(data_dir=tmp_path)))
    try:
        assert rt.embedded is not None
        assert rt.embedded.home == (tmp_path / "prefect").resolve()
        assert os.environ["PREFECT_API_URL"] == rt.api_url
        # Prefect's settings see it too, in any thread
        assert in_fresh_context(PREFECT_API_URL.value) == rt.api_url
    finally:
        rt.stop()
    assert "PREFECT_API_URL" not in os.environ
    assert not in_fresh_context(PREFECT_API_URL.value)


# --- served deployments ------------------------------------------------------------


@pytest.mark.req("SVC-01")
@pytest.mark.req("SVC-02")
@pytest.mark.req("SVC-07")
def test_build_deployments() -> None:
    deps = {d.name: d for d in build_deployments()}
    dep = deps[VERIFY_FILES_DEPLOYMENT]
    assert dep.name == VERIFY_FILES_DEPLOYMENT == "verify-files"
    assert dep.flow_name == "verify_files"
    assert dep.entrypoint == "heliopuddle.server.services.verify.verify_files"
    assert set(deps) == {VERIFY_FILES_DEPLOYMENT, DELETE_FILES_DEPLOYMENT, "rule-once"}
    dep = deps[DELETE_FILES_DEPLOYMENT]
    assert dep.flow_name == "delete_files"
    assert dep.entrypoint == "heliopuddle.server.services.delete.delete_files"


def test_deployment_specs_name_verify_and_delete() -> None:
    assert [(s.module, s.flow, s.name) for s in DEPLOYMENTS] == [
        ("heliopuddle.server.services.verify", "verify_files", "verify-files"),
        ("heliopuddle.server.services.delete", "delete_files", "delete-files"),
        ("heliopuddle.server.services.rules", "run_rule_once", "rule-once"),
    ]


@pytest.mark.req("SVC-07")
def test_build_deployments_serves_delete_files_when_its_module_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from prefect import flow

    @flow(name="delete_files")
    def delete_files(file_ids: list[int]) -> None:
        """Stand-in for the real flow."""

    module = types.ModuleType("heliopuddle.server.services.delete")
    module.delete_files = delete_files  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "heliopuddle.server.services.delete", module)

    deps = {d.name: d for d in build_deployments()}

    assert set(deps) == {VERIFY_FILES_DEPLOYMENT, DELETE_FILES_DEPLOYMENT, "rule-once"}
    assert deps[DELETE_FILES_DEPLOYMENT].flow_name == "delete_files"
    assert deps[DELETE_FILES_DEPLOYMENT].entrypoint.endswith(".delete_files")


def test_build_deployments_skips_a_missing_module() -> None:
    specs = (
        *DEPLOYMENTS[:1],
        DeploymentSpec("heliopuddle.server.services.not_there", "flow", "nope", "missing"),
    )

    assert [d.name for d in build_deployments(specs)] == [VERIFY_FILES_DEPLOYMENT]


def test_build_deployments_reports_other_import_errors() -> None:
    specs = (DeploymentSpec("not_a_package_hp.flows", "flow", "nope", "broken"),)

    with pytest.raises(ModuleNotFoundError, match="not_a_package_hp"):
        build_deployments(specs)


@pytest.mark.req("SVC-01")
@pytest.mark.usefixtures("prefect_harness")
def test_services_runner_registers_and_stops() -> None:
    from prefect.client.orchestration import get_client

    services = ServicesRunner(build_deployments(), query_seconds=1)
    services.start()
    try:
        assert services.is_alive()
        with get_client(sync_client=True) as client:
            dep = client.read_deployment_by_name("verify_files/verify-files")
        assert dep.entrypoint == "heliopuddle.server.services.verify.verify_files"
    finally:
        services.stop()
    assert not services.is_alive()
    assert services.error is None
    services.stop()  # idempotent


class _BadDeployment:
    name = "bad"
    flow_name = "bad"
    storage = None

    async def aapply(self) -> Any:
        raise RuntimeError("registration refused")


@pytest.mark.req("SVC-01")
@pytest.mark.usefixtures("prefect_harness")
def test_services_runner_registration_failure() -> None:
    services = ServicesRunner([_BadDeployment()])  # type: ignore[list-item]
    with pytest.raises(RuntimeError, match="registration refused"):
        services.start()
    assert isinstance(services.error, RuntimeError)
    services.stop()


def test_services_runner_start_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    services = ServicesRunner([])
    monkeypatch.setattr(services, "_thread_main", lambda: None)
    with pytest.raises(RuntimeError, match="did not start"):
        services.start(timeout=0.1)


# --- concurrency ------------------------------------------------------------------


def test_limit_names() -> None:
    assert storage_location_limit("p", "primary") == "storage-location:p:primary"
    assert rule_limit("p", "archive") == "rule:p:archive"


@pytest.mark.usefixtures("prefect_harness")
def test_hp_concurrency_holds_a_global_slot() -> None:
    from prefect.client.orchestration import get_client
    from prefect.client.schemas.actions import GlobalConcurrencyLimitCreate

    name = storage_location_limit("p", f"loc-{uuid.uuid4().hex[:8]}")
    with get_client(sync_client=True) as client:
        client.create_global_concurrency_limit(GlobalConcurrencyLimitCreate(name=name, limit=1))

    with (
        hp_concurrency(name, timeout_seconds=5),
        pytest.raises(TimeoutError),
        hp_concurrency(name, timeout_seconds=1),
    ):
        pass  # pragma: no cover
    with hp_concurrency(name, timeout_seconds=5):
        pass  # the slot was released

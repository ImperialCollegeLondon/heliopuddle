"""The Prefect API this process uses: an external server or an embedded one (SVC-01).

When ``PREFECT_API_URL`` is set, :func:`start_prefect` checks
``{PREFECT_API_URL}/health`` and fails fast if it does not answer. When it is
unset, it starts ``prefect server start`` as a child process with
``PREFECT_HOME`` under ``{data_dir}/prefect``, waits for its ``/health`` and
points this process (and the processes it starts, such as flow runs) at it.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from heliopuddle.settings import Settings

logger = logging.getLogger(__name__)

DEFAULT_PREFECT_PORT = 4200
DEFAULT_PREFECT_HOST = "127.0.0.1"
HEALTH_TIMEOUT_S = 5.0
STARTUP_TIMEOUT_S = 120.0
STOP_TIMEOUT_S = 20.0
LOG_TAIL_LINES = 30


class PrefectStartupError(RuntimeError):
    """The Prefect API is unreachable or the embedded server failed to start."""


def configured_api_url() -> str | None:
    """Return ``PREFECT_API_URL`` as Prefect sees it (env or profile), or None."""
    from prefect.settings import PREFECT_API_URL

    return PREFECT_API_URL.value() or None


def use_api_url(url: str | None) -> None:
    """Point this process, and the child processes it starts, at a Prefect API.

    Sets (or, for None, removes) ``PREFECT_API_URL`` in the environment and
    refreshes Prefect's process-wide settings so that clients created from now
    on, in any thread, use it.

    Args:
        url: the Prefect API URL (ending in ``/api``), or None to unset it.
    """
    from prefect.context import refresh_global_settings_context

    if url is None:
        os.environ.pop("PREFECT_API_URL", None)
    else:
        os.environ["PREFECT_API_URL"] = url
    refresh_global_settings_context()


def check_api(url: str, timeout: float = HEALTH_TIMEOUT_S) -> None:
    """Check that ``{url}/health`` answers 200.

    Args:
        url: the Prefect API URL.
        timeout: seconds to wait for the answer.

    Raises:
        PrefectStartupError: if the server is unreachable or unhealthy.
    """
    health = f"{url.rstrip('/')}/health"
    try:
        response = httpx.get(health, timeout=timeout)
    except httpx.HTTPError as e:
        raise PrefectStartupError(
            f"Prefect API at {url} (PREFECT_API_URL) is unreachable: {type(e).__name__}: {e}"
        ) from None
    if response.status_code != 200:
        raise PrefectStartupError(
            f"Prefect API at {url} (PREFECT_API_URL) is unhealthy: "
            f"GET {health} returned {response.status_code}"
        )


def port_is_free(host: str, port: int) -> bool:
    """Return True if ``host:port`` can be bound now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def free_port(host: str = "127.0.0.1") -> int:
    """Return a TCP port that is free on ``host`` right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def _client_host(host: str) -> str:
    """The address to connect to for a server bound to ``host``."""
    return "127.0.0.1" if host in {"0.0.0.0", "", "::"} else host


@dataclass
class EmbeddedPrefectServer:
    """A ``prefect server start`` child process.

    Attributes:
        home: ``PREFECT_HOME`` of the child (its database and logs live here).
        host: interface the server binds to.
        port: port the server listens on.
        startup_timeout: seconds to wait for ``/health`` after starting.
    """

    home: Path
    host: str = DEFAULT_PREFECT_HOST
    port: int = DEFAULT_PREFECT_PORT
    startup_timeout: float = STARTUP_TIMEOUT_S
    process: subprocess.Popen[bytes] | None = field(default=None, init=False, repr=False)

    @classmethod
    def from_settings(
        cls, settings: Settings, environ: dict[str, str] | None = None
    ) -> EmbeddedPrefectServer:
        """Configure the embedded server from settings and the environment.

        ``PREFECT_HOME`` is ``{data_dir}/prefect``. The host is
        ``PREFECT_SERVER_API_HOST`` (default ``127.0.0.1``). The port is
        ``PREFECT_SERVER_API_PORT`` if set; otherwise 4200, or a free port if
        4200 is taken.

        Args:
            settings: process settings.
            environ: environment to read (defaults to ``os.environ``).
        """
        env = os.environ if environ is None else environ
        host = env.get("PREFECT_SERVER_API_HOST") or DEFAULT_PREFECT_HOST
        if env.get("PREFECT_SERVER_API_PORT"):
            port = int(env["PREFECT_SERVER_API_PORT"])
        elif port_is_free(host, DEFAULT_PREFECT_PORT):
            port = DEFAULT_PREFECT_PORT
        else:
            port = free_port(host)
            logger.info("Port %s is in use; the embedded Prefect server uses %s", 4200, port)
        return cls(home=(settings.data_dir / "prefect").resolve(), host=host, port=port)

    @property
    def api_url(self) -> str:
        """The server's API URL, for clients in this host."""
        return f"http://{_client_host(self.host)}:{self.port}/api"

    @property
    def log_path(self) -> Path:
        """File receiving the child's stdout and stderr."""
        return self.home / "server.log"

    def command(self) -> list[str]:
        """The child's command line."""
        return [
            sys.executable, "-m", "heliopuddle.server.services._prefect_child",
            "server", "start", "--host", self.host, "--port", str(self.port),
        ]  # fmt: skip

    def child_env(self) -> dict[str, str]:
        """The child's environment: ours, minus ``PREFECT_API_URL``, plus its home."""
        env = {k: v for k, v in os.environ.items() if k != "PREFECT_API_URL"}
        env["PREFECT_HOME"] = str(self.home)
        env["PREFECT_SERVER_API_HOST"] = self.host
        env["PREFECT_SERVER_API_PORT"] = str(self.port)
        return env

    def start(self) -> str:
        """Start the server and wait until ``/health`` answers.

        Returns:
            The server's API URL.

        Raises:
            PrefectStartupError: if the port is taken, the process exits or
                ``/health`` does not answer within ``startup_timeout``.
        """
        if not port_is_free(self.host, self.port):
            raise PrefectStartupError(
                f"Cannot start the embedded Prefect server: port {self.port} on {self.host} "
                "is in use (set PREFECT_SERVER_API_PORT, or PREFECT_API_URL to use an "
                "existing server)"
            )
        self.home.mkdir(parents=True, exist_ok=True)
        logger.info(
            "Starting the embedded Prefect server on %s:%s (PREFECT_HOME=%s, log %s)",
            self.host, self.port, self.home, self.log_path,
        )  # fmt: skip
        with self.log_path.open("ab") as log:
            self.process = subprocess.Popen(
                self.command(),
                env=self.child_env(),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self._wait_healthy()
        logger.info("Embedded Prefect server is up at %s", self.api_url)
        return self.api_url

    def _wait_healthy(self) -> None:
        assert self.process is not None
        deadline = time.monotonic() + self.startup_timeout
        while True:
            code = self.process.poll()
            if code is not None:
                self.process = None
                raise PrefectStartupError(
                    f"The embedded Prefect server exited with code {code} during startup. "
                    f"Last lines of {self.log_path}:\n{self.log_tail()}"
                )
            try:
                check_api(self.api_url, timeout=2.0)
                return
            except PrefectStartupError:
                pass
            if time.monotonic() >= deadline:
                self.stop()
                raise PrefectStartupError(
                    f"The embedded Prefect server did not answer {self.api_url}/health within "
                    f"{self.startup_timeout:.0f}s. Last lines of {self.log_path}:\n"
                    f"{self.log_tail()}"
                )
            time.sleep(0.5)

    def log_tail(self, lines: int = LOG_TAIL_LINES) -> str:
        """Return the last lines of the server log (empty if there is none)."""
        try:
            text = self.log_path.read_text(errors="replace")
        except OSError:
            return ""
        return "\n".join(text.splitlines()[-lines:])

    def stop(self, timeout: float = STOP_TIMEOUT_S) -> None:
        """Stop the server, forcing termination after ``timeout`` seconds."""
        process, self.process = self.process, None
        if process is None or process.poll() is not None:
            return
        logger.info("Stopping the embedded Prefect server")
        if sys.platform == "win32":
            process.terminate()
        else:
            _signal_group(process, signal.SIGTERM)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.warning("The embedded Prefect server did not stop; killing it")
            if sys.platform == "win32":
                process.kill()
            else:
                _signal_group(process, signal.SIGKILL)
            process.wait(timeout=timeout)


def _signal_group(process: subprocess.Popen[bytes], sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, sig)


@dataclass
class PrefectRuntime:
    """The Prefect API in use, and how to release it.

    Attributes:
        api_url: the Prefect API URL this process now uses.
        embedded: the embedded server, or None when an external one is used.
    """

    api_url: str
    embedded: EmbeddedPrefectServer | None = None
    _restore: Callable[[], None] | None = field(default=None, repr=False)

    def stop(self) -> None:
        """Stop the embedded server (if any) and restore ``PREFECT_API_URL``."""
        if self.embedded is not None:
            self.embedded.stop()
            self.embedded = None
        if self._restore is not None:
            self._restore()
            self._restore = None


def start_prefect(settings: Settings) -> PrefectRuntime:
    """Use the external Prefect API, or start an embedded one.

    Args:
        settings: process settings (the embedded server's home is under
            ``data_dir``).

    Returns:
        The runtime; call :meth:`PrefectRuntime.stop` on shutdown.

    Raises:
        PrefectStartupError: if the external API is unreachable or the
            embedded server fails to start.
    """
    # Per-file task functions run outside any flow run; keep their logs local
    # instead of warning that they cannot be sent to the API.
    if "PREFECT_LOGGING_TO_API_WHEN_MISSING_FLOW" not in os.environ:
        os.environ["PREFECT_LOGGING_TO_API_WHEN_MISSING_FLOW"] = "ignore"
        from prefect.context import refresh_global_settings_context

        refresh_global_settings_context()

    external = configured_api_url()
    if external:
        logger.info("Using the Prefect API at %s", external)
        check_api(external)
        return PrefectRuntime(api_url=external)

    server = EmbeddedPrefectServer.from_settings(settings)
    url = server.start()
    previous = os.environ.get("PREFECT_API_URL")
    use_api_url(url)
    return PrefectRuntime(api_url=url, embedded=server, _restore=lambda: use_api_url(previous))

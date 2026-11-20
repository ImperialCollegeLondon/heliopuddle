"""The uvicorn servers of ``server start``: specs, shutdown handling and the run loop (DEP-11).

:class:`StartupError` lives here so the run loop can raise it; ``heliopuddle.server.start``
re-exports it with the rest of the startup API.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import FrameType
from typing import Any

import uvicorn
from starlette.types import ASGIApp

from heliopuddle.server.services.deploy import ServicesRunner

logger = logging.getLogger(__name__)

EXIT_FAILURE = 1
EXIT_CONFIG = 2


class StartupError(Exception):
    """Startup failed; the message says why.

    Attributes:
        exit_code: the process exit code (2 for invalid settings or config,
            1 for anything else).
    """

    def __init__(self, message: str, exit_code: int = EXIT_FAILURE) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass
class ServerSpec:
    """One uvicorn server to run.

    Attributes:
        name: label for logs (``api+ui``, ``api``, ``ui``).
        host: interface to bind.
        port: port to bind.
        app: the ASGI app, root path applied.
    """

    name: str
    host: str
    port: int
    app: ASGIApp = field(repr=False)


class _Server(uvicorn.Server):
    """A uvicorn server that leaves signal handling to :class:`Shutdown`."""

    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


class Shutdown:
    """Signal handler that stops the uvicorn servers and wakes the main thread (DEP-11).

    The first signal (or any other stop request) asks every server to stop
    gracefully: they finish their requests and run their lifespan shutdown.
    Repeated stop requests change nothing; only a second *signal* forces the
    servers to exit without waiting.
    """

    def __init__(self) -> None:
        self.event = threading.Event()
        self.servers: list[uvicorn.Server] = []
        self.signals = 0
        self._previous: dict[int, Any] = {}

    @property
    def signalled(self) -> bool:
        """True once a signal arrived (a stop the operator asked for)."""
        return self.signals > 0

    def request(self, reason: str = "shutdown requested") -> None:
        """Ask every part to stop gracefully (idempotent)."""
        if not self.event.is_set():
            logger.info("Shutting down (%s)", reason)
        for server in self.servers:
            server.should_exit = True
        self.event.set()

    def force(self) -> None:
        """Make every server exit at once, without waiting for open requests."""
        logger.warning("Forcing shutdown")
        for server in self.servers:
            server.should_exit = True
            server.force_exit = True
        self.event.set()

    def _handle(self, signum: int, _frame: FrameType | None) -> None:
        self.signals += 1
        if self.signals > 1:
            self.force()
        else:
            self.request(signal.Signals(signum).name)

    def install(self) -> None:
        """Handle SIGINT and SIGTERM (main thread only)."""
        if threading.current_thread() is not threading.main_thread():
            return
        for sig in (signal.SIGINT, signal.SIGTERM):
            self._previous[sig] = signal.signal(sig, self._handle)

    def restore(self) -> None:
        """Restore the previous signal handlers."""
        for sig, handler in self._previous.items():
            signal.signal(sig, handler)
        self._previous.clear()


def run_servers(
    specs: list[ServerSpec], shutdown: Shutdown, services: ServicesRunner | None
) -> None:
    """Run the uvicorn servers until a signal arrives or one of them stops.

    When one server stops (or the served deployments die), the others are
    asked to stop gracefully, once; the loop then waits only on the servers
    still running, so it never spins (DEP-11). Each server runs its lifespan
    shutdown.

    Raises:
        StartupError: if the served deployments stop unexpectedly.
    """
    servers: list[uvicorn.Server] = [
        _Server(uvicorn.Config(spec.app, host=spec.host, port=spec.port, log_level="info"))
        for spec in specs
    ]
    shutdown.servers = servers
    for spec in specs:
        logger.info("Serving %s on http://%s:%s", spec.name, spec.host, spec.port)

    async def main() -> None:
        tasks = [asyncio.create_task(s.serve()) for s in servers]
        pending: set[asyncio.Task[None]] = set(tasks)
        stopping = False
        while pending:
            done, pending = await asyncio.wait(
                pending, timeout=1.0, return_when=asyncio.FIRST_COMPLETED
            )
            if not stopping and (done or _services_died(services)):
                stopping = True
                shutdown.request("a component stopped")
        for task in tasks:
            task.result()

    asyncio.run(main())
    _raise_if_services_died(services, shutdown)


def wait_for_shutdown(shutdown: Shutdown, services: ServicesRunner | None) -> None:
    """Block until a signal arrives or the served deployments stop (no HTTP servers).

    Raises:
        StartupError: if the served deployments stop unexpectedly.
    """
    while not shutdown.event.wait(1.0):
        if _services_died(services):
            break
    _raise_if_services_died(services, shutdown)


def _services_died(services: ServicesRunner | None) -> bool:
    return services is not None and not services.is_alive()


def _raise_if_services_died(services: ServicesRunner | None, shutdown: Shutdown) -> None:
    if _services_died(services) and not shutdown.signalled:
        assert services is not None
        raise StartupError(f"The served deployments stopped unexpectedly: {services.error!r}")

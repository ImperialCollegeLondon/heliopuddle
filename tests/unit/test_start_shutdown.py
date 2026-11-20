"""DEP-11: with the UI on its own port, one signal stops both servers gracefully, once.

Two real uvicorn servers run in a thread (as ``server start`` runs them with
``HELIOPUDDLE_UI_PORT`` set); each app records its lifespan.
"""

from __future__ import annotations

import asyncio
import signal
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from heliopuddle.server.services.runtime import free_port
from heliopuddle.server.start import ServerSpec, Shutdown, run_servers

pytestmark = pytest.mark.req("DEP-11")


class _Lifespans:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.lock = threading.Lock()

    def app(self, name: str) -> FastAPI:
        @asynccontextmanager
        async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
            with self.lock:
                self.events.append(f"{name} started")
            yield
            with self.lock:
                self.events.append(f"{name} stopped")

        app = FastAPI(lifespan=lifespan)

        @app.get("/ping")
        def ping() -> str:
            return name

        return app


class _Run:
    """``run_servers`` in a thread, counting the main loop's waits."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.lifespans = _Lifespans()
        self.specs = [
            ServerSpec("api", "127.0.0.1", free_port(), self.lifespans.app("api")),
            ServerSpec("ui", "127.0.0.1", free_port(), self.lifespans.app("ui")),
        ]
        self.shutdown = Shutdown()
        self.waits = 0
        real_wait = asyncio.wait

        async def counting_wait(*args: Any, **kwargs: Any) -> Any:
            self.waits += 1
            return await real_wait(*args, **kwargs)

        monkeypatch.setattr(asyncio, "wait", counting_wait)
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._main, daemon=True)

    def _main(self) -> None:
        try:
            run_servers(self.specs, self.shutdown, None)
        except BaseException as e:  # pragma: no cover - asserted by the tests
            self.error = e

    def start(self) -> None:
        self.thread.start()
        for spec in self.specs:
            deadline = time.monotonic() + 15
            while True:
                try:
                    r = httpx.get(f"http://127.0.0.1:{spec.port}/ping", timeout=1)
                    assert r.json() == spec.name
                    break
                except httpx.HTTPError:
                    assert time.monotonic() < deadline, f"{spec.name} did not start"
                    time.sleep(0.05)

    def join(self) -> None:
        self.thread.join(20)
        assert not self.thread.is_alive(), "run_servers did not return"
        assert self.error is None, self.error


def test_one_signal_stops_both_servers_gracefully_once(monkeypatch: pytest.MonkeyPatch) -> None:
    run = _Run(monkeypatch)
    run.start()

    run.shutdown._handle(signal.SIGTERM, None)  # pyright: ignore[reportPrivateUsage]
    run.join()

    assert sorted(run.lifespans.events) == [
        "api started",
        "api stopped",
        "ui started",
        "ui stopped",
    ]
    servers = run.shutdown.servers
    assert len(servers) == 2
    assert all(s.should_exit and not s.force_exit for s in servers)
    assert run.shutdown.signals == 1 and run.shutdown.signalled
    # The loop waits on unfinished servers only: a handful of waits, no spinning.
    assert run.waits <= 5, run.waits


def test_a_server_stopping_on_its_own_stops_the_other_without_forcing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _Run(monkeypatch)
    run.start()

    run.shutdown.servers[0].should_exit = True  # e.g. the API server failed
    time.sleep(1.5)  # the UI server keeps the loop waiting while it stops
    run.join()

    assert "api stopped" in run.lifespans.events and "ui stopped" in run.lifespans.events
    assert not any(s.force_exit for s in run.shutdown.servers)
    assert not run.shutdown.signalled
    assert run.waits <= 6, run.waits


def test_second_signal_forces_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    run = _Run(monkeypatch)
    run.start()

    run.shutdown._handle(signal.SIGINT, None)  # pyright: ignore[reportPrivateUsage]
    run.shutdown._handle(signal.SIGINT, None)  # pyright: ignore[reportPrivateUsage]
    run.join()

    assert all(s.force_exit for s in run.shutdown.servers)
    assert run.shutdown.signals == 2


def test_repeated_stop_requests_never_force() -> None:
    shutdown = Shutdown()

    class Server:
        should_exit = False
        force_exit = False

    server = Server()
    shutdown.servers = [server]  # type: ignore[list-item]
    for _ in range(5):
        shutdown.request("a component stopped")

    assert server.should_exit and not server.force_exit
    assert not shutdown.signalled

"""App assembly, root paths, server planning and shutdown for ``server start``."""

from __future__ import annotations

import signal
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from heliopuddle.client import HelioPuddleClient
from heliopuddle.server import app as app_mod
from heliopuddle.server import start as start_mod
from heliopuddle.server.api.files import register as register_api
from heliopuddle.server.app import (
    RootPathMiddleware,
    create_app,
    normalize_root_path,
    wrap_root_path,
)
from heliopuddle.server.context import AppContext
from heliopuddle.server.services.runtime import free_port
from heliopuddle.server.start import (
    Shutdown,
    StartOptions,
    StartupError,
    plan_servers,
    run_servers,
    wait_for_shutdown,
)
from heliopuddle.settings import Settings


@pytest.fixture
def no_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the inline verify so these tests need no Prefect server."""
    monkeypatch.setattr(register_api.verify_file, "fn", lambda _id: None)


def _ui_factory(http: httpx.Client) -> Any:
    def factory(project: str | None, user: str | None) -> HelioPuddleClient:
        return HelioPuddleClient("http://testserver/api/v1", project, user=user, http_client=http)

    return factory


# --- DEP-02 / UI-03: app assembly -------------------------------------------------


@pytest.mark.req("DEP-02")
@pytest.mark.req("UI-03")
def test_shared_port_serves_api_and_ui(
    settings: Settings, ctx: AppContext, store: Path, no_verify: None
) -> None:
    (store / "a.txt").write_text("a")
    holder: dict[str, Any] = {}
    app = create_app(settings, ctx, client_factory=lambda p, u: _ui_factory(holder["api"])(p, u))
    with (
        TestClient(app) as http,
        TestClient(app, base_url="http://testserver/api/v1") as api,
    ):
        holder["api"] = api
        assert http.get("/api/v1/version").status_code == 200
        r = http.post(
            "/api/v1/projects/demo/files",
            json={"path": "a.txt", "hash": "0cc175b9c0f1b6a831c399e269772661", "size": 1},
        )
        assert r.status_code == 201, r.text
        page = http.get("/", follow_redirects=True)
        assert page.status_code == 200
        assert "a.txt" in page.text
        assert http.get("/static/hp.css").status_code == 200


@pytest.mark.req("DEP-02")
def test_api_only_has_no_ui(settings: Settings, ctx: AppContext) -> None:
    with TestClient(create_app(settings, ctx, ui=False)) as http:
        assert http.get("/api/v1/version").status_code == 200
        assert http.get("/").status_code == 404


@pytest.mark.req("UI-03")
def test_ui_only_app_has_no_api(settings: Settings) -> None:
    app = create_app(settings, None, api=False)
    assert [getattr(r, "path", None) for r in app.routes] == [""]
    with TestClient(app) as http:
        assert http.get("/api/v1/version").status_code == 404
        assert http.get("/static/hp.css").status_code == 200


def test_create_app_rejects_bad_combinations(settings: Settings) -> None:
    with pytest.raises(ValueError, match="API, the UI or both"):
        create_app(settings, None, api=False, ui=False)
    with pytest.raises(ValueError, match="AppContext"):
        create_app(settings, None, api=True, ui=False)


def test_mounted_app_lifespans_run(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        events.append("start")
        yield
        events.append("stop")

    monkeypatch.setattr(app_mod, "create_ui", lambda s, f=None: FastAPI(lifespan=lifespan))
    with TestClient(create_app(settings, None, api=False)):
        assert events == ["start"]
    assert events == ["start", "stop"]


# --- UI-03: root paths ------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"), [("", ""), ("/", ""), ("hp", "/hp"), ("/hp/", "/hp"), (" /a/b ", "/a/b")]
)
def test_normalize_root_path(raw: str, expected: str) -> None:
    assert normalize_root_path(raw) == expected


@pytest.mark.req("UI-03")
def test_root_path_accepts_prefixed_and_stripped_requests(
    settings: Settings, ctx: AppContext
) -> None:
    app = wrap_root_path(create_app(settings, ctx, ui=False), "/hp/")
    assert isinstance(app, RootPathMiddleware)
    with TestClient(app) as http:
        assert http.get("/hp/api/v1/version").status_code == 200
        assert http.get("/api/v1/version").status_code == 200
        assert http.get("/hpx/api/v1/version").status_code == 404


@pytest.mark.req("UI-03")
def test_root_path_prefixes_generated_urls() -> None:
    inner = FastAPI()

    @inner.get("/where", name="where")
    def where(request: Request) -> dict[str, str]:
        return {"url": str(request.url_for("where")), "root": request.scope["root_path"]}

    with TestClient(wrap_root_path(inner, "/proxy")) as http:
        body = http.get("/where").json()
    assert body == {"url": "http://testserver/proxy/where", "root": "/proxy"}


def test_no_root_path_leaves_app_unwrapped(settings: Settings) -> None:
    inner = FastAPI()
    assert wrap_root_path(inner, "") is inner


# --- UI-03: which servers run -----------------------------------------------------


def _settings(tmp_path: Path, **kw: Any) -> Settings:
    return Settings(config_dir=tmp_path, data_dir=tmp_path / "data", **kw)


@pytest.mark.req("UI-03")
def test_plan_shared_port(tmp_path: Path, ctx: AppContext) -> None:
    s = _settings(tmp_path, port=9000, api_root_path="/hp")
    [spec] = plan_servers(s, ctx, StartOptions())
    assert (spec.name, spec.host, spec.port) == ("api+ui", "127.0.0.1", 9000)
    assert isinstance(spec.app, RootPathMiddleware)
    assert spec.app.root_path == "/hp"


@pytest.mark.req("UI-03")
def test_plan_ui_on_own_port(tmp_path: Path, ctx: AppContext) -> None:
    s = _settings(tmp_path, port=9000, ui_port=9001, ui_host="0.0.0.0", ui_root_path="/ui")
    api, ui = plan_servers(s, ctx, StartOptions())
    assert (api.name, api.port) == ("api", 9000)
    assert not isinstance(api.app, RootPathMiddleware)
    assert (ui.name, ui.host, ui.port) == ("ui", "0.0.0.0", 9001)
    assert isinstance(ui.app, RootPathMiddleware)
    assert ui.app.root_path == "/ui"


@pytest.mark.req("UI-03")
def test_plan_ui_port_equal_to_port_shares_it(tmp_path: Path, ctx: AppContext) -> None:
    s = _settings(tmp_path, port=9000, ui_port=9000)
    assert [x.name for x in plan_servers(s, ctx, StartOptions())] == ["api+ui"]


@pytest.mark.req("UI-03")
def test_plan_ui_only(tmp_path: Path) -> None:
    s = _settings(tmp_path, port=9000, ui_api_url="http://elsewhere/api/v1")
    [spec] = plan_servers(s, None, StartOptions(api=False, services=False))
    assert (spec.name, spec.port) == ("ui", 9000)


# --- DEP-02: shutdown -------------------------------------------------------------


class _FakeServer:
    should_exit = False
    force_exit = False


@pytest.mark.req("DEP-02")
def test_shutdown_stops_servers_and_second_signal_forces() -> None:
    shutdown = Shutdown()
    server = _FakeServer()
    shutdown.servers = [server]  # type: ignore[list-item]

    shutdown._handle(signal.SIGTERM, None)
    assert shutdown.event.is_set()
    assert server.should_exit and not server.force_exit

    shutdown._handle(signal.SIGINT, None)
    assert server.force_exit


def test_shutdown_installs_and_restores_handlers() -> None:
    before = signal.getsignal(signal.SIGTERM)
    shutdown = Shutdown()
    shutdown.install()
    try:
        assert signal.getsignal(signal.SIGTERM) == shutdown._handle
    finally:
        shutdown.restore()
    assert signal.getsignal(signal.SIGTERM) == before


def test_shutdown_install_off_main_thread_is_a_no_op() -> None:
    shutdown = Shutdown()
    t = threading.Thread(target=shutdown.install)
    t.start()
    t.join()
    assert shutdown._previous == {}


class _Services:
    def __init__(self, alive: bool = True) -> None:
        self.alive = alive
        self.error: BaseException | None = RuntimeError("runner crashed") if not alive else None

    def is_alive(self) -> bool:
        return self.alive


@pytest.fixture
def two_specs() -> Iterator[list[start_mod.ServerSpec]]:
    app = FastAPI()

    @app.get("/ping")
    def ping() -> str:
        return "pong"

    yield [
        start_mod.ServerSpec("api", "127.0.0.1", free_port(), app),
        start_mod.ServerSpec("ui", "127.0.0.1", free_port(), app),
    ]


@pytest.mark.req("DEP-02")
@pytest.mark.req("UI-03")
def test_run_servers_until_shutdown(two_specs: list[start_mod.ServerSpec]) -> None:
    shutdown = Shutdown()
    answers: list[int] = []

    def client() -> None:
        for spec in two_specs:
            for _ in range(100):
                try:
                    r = httpx.get(f"http://127.0.0.1:{spec.port}/ping", timeout=1)
                    answers.append(r.status_code)
                    break
                except httpx.HTTPError:
                    threading.Event().wait(0.05)
        shutdown.request("test done")

    t = threading.Thread(target=client)
    t.start()
    run_servers(two_specs, shutdown, _Services())
    t.join()
    assert answers == [200, 200]


@pytest.mark.req("DEP-02")
def test_run_servers_stop_when_services_die(two_specs: list[start_mod.ServerSpec]) -> None:
    with pytest.raises(StartupError, match="stopped unexpectedly"):
        run_servers(two_specs, Shutdown(), _Services(alive=False))  # type: ignore[arg-type]


@pytest.mark.req("DEP-02")
def test_wait_for_shutdown_returns_on_signal() -> None:
    shutdown = Shutdown()
    threading.Timer(0.1, shutdown.request).start()
    wait_for_shutdown(shutdown, _Services())  # type: ignore[arg-type]
    assert shutdown.event.is_set()


@pytest.mark.req("DEP-02")
def test_wait_for_shutdown_fails_when_services_die() -> None:
    with pytest.raises(StartupError, match="runner crashed"):
        wait_for_shutdown(Shutdown(), _Services(alive=False))  # type: ignore[arg-type]

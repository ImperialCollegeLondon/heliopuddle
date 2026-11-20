"""Root path, default client factory, static assets and import rules of the web UI."""

from __future__ import annotations

import ast
import os
from typing import Any

import pytest
from fastapi.testclient import TestClient

from heliopuddle.server.ui import (
    create_ui,
    default_api_url,
)
from heliopuddle.settings import Settings
from tests.unit import _ui_helpers as _fixtures
from tests.unit._ui_helpers import FORBIDDEN, UI_DIR, Factory, StubClient, make_file, ui_client

_no_prefect_env = _fixtures._no_prefect_env


@pytest.mark.req("UI-03")
def test_root_path_is_used_in_redirects_and_links() -> None:
    factory = Factory(projects=["demo"], files=[make_file(i) for i in range(1, 30)])
    client = ui_client(factory, ui_root_path="/hp")
    redirect = client.get("/hp/", follow_redirects=False)
    assert redirect.headers["location"] == "/hp/projects/demo"

    page = client.get("/hp/projects/demo/files", params={"limit": 25})
    assert page.status_code == 200
    assert 'href="/hp/static/hp.css"' in page.text
    assert 'src="/hp/static/hp.js"' in page.text
    assert "/hp/projects/demo/files?limit=25&amp;offset=25" in page.text
    assert 'href="/hp/"' in page.text
    assert 'action="/hp/search"' in page.text


@pytest.mark.req("UI-03")
def test_root_path_on_the_home_page() -> None:
    client = ui_client(Factory(projects=["a", "b"]), ui_root_path="/proxy/ui")
    page = client.get("/proxy/ui/")
    assert 'href="/proxy/ui/projects/a"' in page.text


@pytest.mark.req("UI-03")
def test_root_path_is_set_on_the_app() -> None:
    app = create_ui(Settings(auth_mode="header", ui_root_path="/hp/"), client_factory=Factory())
    assert app.state.root_path == "/hp"
    with TestClient(app) as client:  # also runs lifespan through the middleware
        page = client.get("/projects/demo")  # the proxy already stripped the prefix
    assert 'href="/hp/static/hp.css"' in page.text


@pytest.mark.req("UI-03")
@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        ({}, "http://127.0.0.1:8080/api/v1"),
        ({"port": 9000, "api_root_path": "/hp"}, "http://127.0.0.1:9000/hp/api/v1"),
        ({"api_root_path": "/hp/"}, "http://127.0.0.1:8080/hp/api/v1"),
        ({"ui_api_url": "https://api.example/api/v1/"}, "https://api.example/api/v1"),
    ],
)
def test_default_api_url(settings: dict[str, Any], expected: str) -> None:
    assert default_api_url(Settings(**settings)) == expected


@pytest.mark.req("UI-03")
def test_default_client_factory_uses_api_url_and_user_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import heliopuddle.server.ui.shared.clients as ui_clients

    seen: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def fake_client(*args: Any, **kwargs: Any) -> StubClient:
        seen.append((args, kwargs))
        return StubClient(kwargs.get("project", args[1] if len(args) > 1 else None), None)

    monkeypatch.setattr(ui_clients, "HelioPuddleClient", fake_client)
    settings = Settings(auth_mode="header", port=9000, ui_api_url=None, user_header="X-User")
    client = TestClient(create_ui(settings))
    resp = client.get("/projects/demo", headers={"X-User": "carol"})
    assert resp.status_code == 200
    args, kwargs = seen[0]
    assert args == ("http://127.0.0.1:9000/api/v1", "demo")
    assert kwargs["user"] == "carol"
    assert kwargs["user_header"] == "X-User"


@pytest.mark.req("UI-03", "UI-09")
def test_static_assets_are_served_from_the_package() -> None:
    client = ui_client(Factory())
    for name, kind in (
        ("hp.css", "text/css"),
        ("hp.js", "javascript"),
        ("icon.svg", "image/svg+xml"),
        ("fonts/manrope-latin-wght.woff2", "font/woff2"),
        ("fonts/ibm-plex-sans-latin-wght.woff2", "font/woff2"),
        ("fonts/ibm-plex-mono-latin-400.woff2", "font/woff2"),
        ("fonts/ibm-plex-mono-latin-500.woff2", "font/woff2"),
    ):
        resp = client.get(f"/static/{name}")
        assert resp.status_code == 200, name
        assert kind in resp.headers["content-type"], name


@pytest.mark.req("UI-05")
def test_ui_does_not_import_the_db_layer() -> None:
    sources = sorted(UI_DIR.rglob("*.py"))
    assert sources
    for source in sources:
        tree = ast.parse(source.read_text(), filename=os.fspath(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert not name.startswith(FORBIDDEN), f"{source.name} imports {name}"


@pytest.mark.req("UI-05")
def test_import_linter_contract_covers_ui() -> None:
    import tomllib

    pyproject = tomllib.loads((UI_DIR.parents[3] / "pyproject.toml").read_text())
    contracts = pyproject["tool"]["importlinter"]["contracts"]
    ui = [c for c in contracts if "heliopuddle.server.ui" in c["source_modules"]]
    assert ui
    forbidden = set(ui[0]["forbidden_modules"])
    assert {"heliopuddle.server.database", "heliopuddle.server.models", "sqlalchemy"} <= forbidden

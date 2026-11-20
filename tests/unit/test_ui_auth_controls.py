"""Role-aware controls and header mode in the UI (AUTH-16, AUTH-06)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from heliopuddle.server.schemas import FileState
from heliopuddle.server.ui import create_ui
from heliopuddle.settings import Settings
from tests.unit._ui_auth_helpers import PASSWORD, AuthFactory, login, ui
from tests.unit._ui_helpers import (
    Factory,
    make_file,
    series_files,
)

pytestmark = pytest.mark.req("AUTH-16")


def unknown_copy() -> list[Any]:
    files = series_files()
    for f in files:
        for copy in f.storage_locations:
            copy.state = FileState.UNKNOWN
    return files


CONTROLS = {
    "index": ("/projects/demo", "Index now"),
    "reconcile": ("/projects/demo", "Reconcile now"),
    "ignore": ("/projects/demo/files", "Ignore from all rules"),
    "index_page": ("/projects/demo/storage/primary", "Index · full"),
    "inspector": ("/projects/demo/files?file=2", "Accept as is"),
    "accept": ("/projects/demo/files/2", "Accept as is"),
    "ignore_detail": ("/projects/demo/files/2", "Ignore from all rules"),
    "rules": ("/projects/demo/rules", "Run now"),
    "disable": ("/projects/demo/rules", "Disable"),
    "once": ("/projects/demo/rules", "Run once"),
}

NEEDS = {
    "index": "operator",
    "reconcile": "operator",
    "ignore": "writer",
    "index_page": "operator",
    "inspector": "writer",
    "accept": "writer",
    "ignore_detail": "writer",
    "rules": "operator",
    "disable": "operator",
    "once": "operator",
}


def test_the_project_picker_shows_what_the_api_returns() -> None:
    factory = AuthFactory(projects=["alpha", "beta"])
    browser = ui(factory)
    login(browser)
    page = browser.get("/")
    assert 'href="/projects/alpha"' in page.text
    assert 'href="/projects/beta"' in page.text
    factory.projects = []
    assert "No projects are configured" in browser.get("/").text


@pytest.mark.parametrize(
    ("roles", "granted"),
    [
        (["hp-reader"], set()),
        (["hp-writer"], {"writer"}),
        (["hp-operator"], {"operator"}),
        (["hp-writer", "hp-operator"], {"writer", "operator"}),
        (["hp-deleter"], set()),
        (["hp-admin"], {"writer", "operator"}),
        (["hp-writer:demo", "hp-operator:other"], {"writer"}),
        (["hp-operator:demo"], {"operator"}),
        (["hp-writer:other"], set()),
    ],
)
def test_write_controls_follow_the_roles_on_the_project(
    roles: list[str], granted: set[str]
) -> None:
    from tests.unit.test_ui_rules import rule

    factory = AuthFactory(users={"ann": (PASSWORD, roles)}, files=unknown_copy(), rules=[rule()])
    browser = ui(factory)
    login(browser)
    for control, (path, text) in CONTROLS.items():
        html = browser.get(path).text
        assert (text in html) == (NEEDS[control] in granted), (roles, control)


@pytest.mark.req("AUTH-16")
def test_controls_stay_visible_in_header_mode() -> None:
    from tests.unit._ui_helpers import ui_client
    from tests.unit.test_ui_rules import rule

    browser = ui_client(Factory(files=unknown_copy(), rules=[rule()]))
    for path, text in CONTROLS.values():
        assert text in browser.get(path).text


def test_header_mode_has_no_login_session_or_auth_chrome(tmp_path: Path) -> None:
    factory = AuthFactory(files=[make_file(1)])
    settings = Settings(auth_mode="header", data_dir=tmp_path / "data", user_header="X-User")
    browser = TestClient(create_ui(settings, client_factory=factory), raise_server_exceptions=False)
    page = browser.get("/projects/demo", headers={"X-User": "bob"})
    assert page.status_code == 200
    assert "Log out" not in page.text
    assert "set-cookie" not in page.headers
    assert factory.made[0].user == "bob"
    assert factory.made[0].token is None
    assert browser.get("/login").status_code == 404
    assert browser.post("/logout").status_code in (404, 405)
    assert not (tmp_path / "data").exists()  # no auth secret was generated

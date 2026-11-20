"""UI sessions, logout and expiry (AUTH-16, AUTH-06)."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from heliopuddle.client import (
    AuthenticationError,
    NotFoundError,
    PermissionDeniedError,
)
from heliopuddle.server.ui.auth import SESSION_COOKIE
from heliopuddle.server.ui.auth import session as ui_auth
from tests.unit._ui_auth_helpers import AuthFactory, location, login, ui, with_roles
from tests.unit._ui_helpers import (
    make_file,
)

pytestmark = pytest.mark.req("AUTH-16")


def test_nav_shows_user_roles_and_logout() -> None:
    _, browser = with_roles("hp-reader", "hp-operator:demo")
    html = browser.get("/projects/demo").text
    assert 'data-user="ann"' in html
    assert '<span class="role">hp-reader</span><span class="role">hp-operator:demo</span>' in html
    assert 'action="/logout"' in html
    assert ">Log out</button>" in html


def test_logout_clears_the_session() -> None:
    _, browser = with_roles("hp-reader")
    response = browser.post("/logout", follow_redirects=False)
    assert location(response) == "/login?logged_out=1"
    assert SESSION_COOKIE in response.headers["set-cookie"]
    assert "1970" in response.headers["set-cookie"]  # expired: the browser drops it
    assert location(browser.get("/projects/demo", follow_redirects=False)) == (
        "/login?next=%2Fprojects%2Fdemo"
    )
    assert "You have been logged out." in browser.get("/login?logged_out=1").text
    assert "Log out" not in browser.get("/login").text


def test_logout_and_cross_site_logout() -> None:
    _, browser = with_roles("hp-reader")
    assert browser.post("/logout", headers={"Origin": "https://evil.example"}).status_code == 403
    assert browser.get("/projects/demo").status_code == 200  # still logged in


def test_tampered_session_cookie_is_ignored() -> None:
    _, browser = with_roles("hp-reader")
    browser.cookies.clear()
    browser.cookies.set(SESSION_COOKIE, "forged.value.here")
    assert location(browser.get("/", follow_redirects=False)) == "/login"


def test_session_signed_with_another_key_is_ignored() -> None:
    _, browser = with_roles("hp-reader")
    cookie = browser.cookies.get(SESSION_COOKIE)
    assert cookie
    other = ui(AuthFactory(), auth_secret_key="y" * 40)
    other.cookies.set(SESSION_COOKIE, cookie)
    assert location(other.get("/", follow_redirects=False)) == "/login"


def test_an_expired_session_redirects_to_login() -> None:
    factory = AuthFactory(ttl=-5)
    browser = ui(factory)
    assert location(login(browser, next="/projects/demo")) == "/projects/demo"
    response = browser.get("/projects/demo", follow_redirects=False)
    assert location(response) == "/login?next=%2Fprojects%2Fdemo"
    assert "Incorrect" not in browser.get("/login").text
    assert factory.api_calls() == []


def test_a_session_expires_when_its_time_runs_out(monkeypatch: pytest.MonkeyPatch) -> None:
    factory = AuthFactory(ttl=60)
    browser = ui(factory)
    login(browser)
    assert browser.get("/projects/demo").status_code == 200
    real = time.time
    later = SimpleNamespace(time=lambda: real() + 3600)
    monkeypatch.setattr(ui_auth, "time", later)
    assert location(browser.get("/projects/demo", follow_redirects=False)).startswith("/login")


def test_an_api_401_clears_the_session_and_logs_in_again() -> None:
    factory = AuthFactory(files=[make_file(1)])
    browser = ui(factory)
    login(browser)
    factory.revoked.update(factory.identities)  # the API stops accepting the token
    response = browser.get("/projects/demo?limit=7", follow_redirects=False)
    assert location(response) == "/login?next=%2Fprojects%2Fdemo%3Flimit%3D7"
    assert "1970" in response.headers["set-cookie"]
    n = len(factory.made)
    assert location(browser.get("/projects/demo", follow_redirects=False)).startswith("/login")
    assert len(factory.made) == n  # no more API calls with the dead token


def test_an_api_401_on_a_post_returns_to_the_root_after_login() -> None:
    factory = AuthFactory(action_error=AuthenticationError(401, "expired"))
    browser = ui(factory)
    login(browser)
    response = browser.post(
        "/projects/demo/storage/primary/index",
        data={"mode": "full"},
        follow_redirects=False,
    )
    assert location(response) == "/login"


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (PermissionDeniedError("insufficient role", ["hp-reader"]), 403),
        (NotFoundError(404, "project 'nope' not found"), 404),
    ],
)
def test_api_403_and_404_show_the_error_page_and_keep_the_session(
    error: Exception, status: int
) -> None:
    factory = AuthFactory()
    browser = ui(factory)
    login(browser)
    factory.error = error
    response = browser.get("/projects/demo", follow_redirects=False)
    assert response.status_code == status
    assert f"Error {status}" in response.text
    factory.error = None
    assert browser.get("/projects/demo").status_code == 200


def test_an_unexpected_error_still_renders_the_error_page() -> None:
    factory = AuthFactory()
    browser = ui(factory, ui_root_path="/hp")
    login(browser)
    factory.error = RuntimeError("boom")
    response = browser.get("/hp/projects/demo")
    assert response.status_code == 500
    assert "Something went wrong." in response.text
    assert "boom" not in response.text

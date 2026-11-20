"""UI login throttling (AUTH-16, AUTH-06)."""

from __future__ import annotations

import pytest

from heliopuddle.client import (
    AuthenticationError,
)
from heliopuddle.settings import Settings
from tests.unit._ui_auth_helpers import SECRET, AuthFactory, login, ui

pytestmark = pytest.mark.req("AUTH-16")


@pytest.mark.req("AUTH-18")
def test_ui_login_is_throttled_per_client_address() -> None:
    from heliopuddle.server.auth.throttle import get_throttle

    clock = [1000.0]
    settings = {"login_max_failures_per_ip": 3}
    browser = ui(AuthFactory(), **settings)
    get_throttle(
        Settings(**{"auth_mode": "local", "auth_secret_key": SECRET, **settings})
    ).clock = lambda: clock[0]
    for _ in range(3):
        assert login(browser, password="not-the-password-1").status_code == 401
    blocked = login(browser)  # the right password is refused too
    assert blocked.status_code == 429
    assert "Too many failed logins" in blocked.text
    assert "set-cookie" not in blocked.headers
    assert int(blocked.headers["retry-after"]) > 0
    clock[0] += 15 * 60 + 1
    assert login(browser).status_code == 303


@pytest.mark.req("AUTH-18")
def test_ui_login_shows_the_apis_throttle_as_a_friendly_error() -> None:
    factory = AuthFactory(login_error=AuthenticationError(429, "too many failed logins"))
    response = login(ui(factory))
    assert response.status_code == 429
    assert "Too many failed logins" in response.text

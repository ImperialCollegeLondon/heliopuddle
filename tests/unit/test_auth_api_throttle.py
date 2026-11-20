"""Token throttling and header mode requests (AUTH-04, AUTH-11)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from heliopuddle.server.api import create_api
from heliopuddle.server.auth.roles import ROLE_READER
from heliopuddle.server.context import AppContext
from tests.local_auth import PASSWORD, add_user, local_context
from tests.unit import _auth_api_helpers as _fixtures
from tests.unit._auth_api_helpers import password_form, post_token

client = _fixtures.client
lctx = _fixtures.lctx


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.req("AUTH-18")
def test_token_throttles_failures_per_username_and_resets_on_success(
    client: TestClient, lctx: AppContext
) -> None:
    from heliopuddle.server.auth.throttle import get_throttle

    clock = _Clock()
    get_throttle(lctx.settings).clock = clock
    limit = lctx.settings.login_max_failures
    assert limit == 10
    for _ in range(limit - 1):
        assert post_token(client, **password_form("ann", "wrong-password-1")).status_code == 400
    # A success before the limit resets the username's counter.
    assert post_token(client, **password_form("ann", PASSWORD)).status_code == 200
    for _ in range(limit):
        assert post_token(client, **password_form("ANN", "wrong-password-1")).status_code == 400
    blocked = post_token(client, **password_form("ann", PASSWORD))  # even the right password
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) > 0
    assert blocked.json()["error"] == "too_many_requests"
    assert blocked.headers["cache-control"] == "no-store"
    # Other usernames are not affected (the per-address limit is higher).
    assert post_token(client, **password_form("bob", "wrong-password-1")).status_code == 400
    clock.now += 15 * 60 + 1  # the window ends
    assert post_token(client, **password_form("ann", PASSWORD)).status_code == 200


@pytest.mark.req("AUTH-18")
def test_token_throttles_failures_per_client_address(
    tmp_path: Path, config_dir: Path, migrated_db: Path
) -> None:
    from heliopuddle.server.auth.throttle import get_throttle

    ctx = local_context(
        tmp_path, config_dir, migrated_db, login_max_failures=50, login_max_failures_per_ip=3
    )
    try:
        add_user(ctx, "ann", [ROLE_READER])
        get_throttle(ctx.settings).clock = _Clock()
        with TestClient(create_api(ctx)) as c:
            for name in ("u1", "u2", "u3"):  # different usernames, one address
                assert post_token(c, **password_form(name, "wrong-password-1")).status_code == 400
            assert post_token(c, **password_form("ann", PASSWORD)).status_code == 429
    finally:
        ctx.dispose()


@pytest.mark.req("AUTH-18")
def test_throttle_settings_take_human_durations(tmp_path: Path) -> None:
    from heliopuddle.settings import Settings

    s = Settings(data_dir=tmp_path, login_window="45m", login_max_failures=3)
    assert s.login_window.total_seconds() == 45 * 60
    with pytest.raises(ValueError):
        Settings(data_dir=tmp_path, login_max_failures=0)


@pytest.mark.req("AUTH-20")
def test_header_mode_rejects_requests_without_the_user_header(
    tmp_path: Path, config_dir: Path, migrated_db: Path
) -> None:
    ctx = local_context(tmp_path, config_dir, migrated_db, auth_mode="header")
    try:
        with TestClient(create_api(ctx), headers={}) as c:
            for headers in ({}, {"X-Forwarded-User": ""}, {"X-Forwarded-User": "  "}):
                for path in ("/auth/me", "/projects"):
                    r = c.get(path, headers=headers)
                    assert r.status_code == 401, path
                    assert r.headers["www-authenticate"] == "Bearer"
            assert c.get("/projects", headers={"X-Forwarded-User": "ok"}).status_code == 200
            # Public routes stay public.
            assert c.get("/version").status_code == 200
            assert c.get("/auth/config").status_code == 200
    finally:
        ctx.dispose()

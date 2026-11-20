"""Password resets racing token issue and password changes (AUTH-04, AUTH-11)."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from heliopuddle.server.api.auth import password as password_api
from heliopuddle.server.api.auth import token as token_api
from heliopuddle.server.auth import passwords
from heliopuddle.server.auth.roles import ROLE_ADMIN, ROLE_READER
from heliopuddle.server.context import AppContext
from heliopuddle.server.models import users as users_db
from tests.local_auth import PASSWORD, add_user, bearer, login
from tests.unit import _auth_api_helpers as _fixtures
from tests.unit._auth_api_helpers import post_token

client = _fixtures.client
lctx = _fixtures.lctx


def _reset_password(ctx: AppContext, username: str, new: str) -> None:
    with ctx.write_session() as session:
        user = users_db.get_user_by_username(session, username)
        assert user is not None
        users_db.set_password_hash(session, user, passwords.hash_password(new))


@pytest.mark.req("AUTH-05")
def test_a_password_reset_between_the_check_and_the_issue_gets_no_token(
    client: TestClient, lctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = token_api.verify_password

    def verify_then_reset(password: str, hashed: str) -> bool:
        ok = real(password, hashed)  # the old password is right...
        _reset_password(lctx, "ann", "a brand new password")  # ...and is reset right after
        return ok

    monkeypatch.setattr(token_api, "verify_password", verify_then_reset)
    r = post_token(client, grant_type="password", username="ann", password=PASSWORD)
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_grant"
    assert "access_token" not in r.text


@pytest.mark.req("AUTH-05")
def test_a_user_recreated_between_the_check_and_the_issue_gets_no_token(
    client: TestClient, lctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = token_api.verify_password

    def verify_then_recreate(password: str, hashed: str) -> bool:
        ok = real(password, hashed)
        with lctx.write_session() as session:
            user = users_db.get_user_by_username(session, "ann")
            assert user is not None
            session.delete(user)
        add_user(lctx, "Ann", [ROLE_ADMIN], password="someone else's password")
        return ok

    monkeypatch.setattr(token_api, "verify_password", verify_then_recreate)
    r = post_token(client, grant_type="password", username="ann", password=PASSWORD)
    assert r.status_code == 400
    assert "access_token" not in r.text


@pytest.mark.req("AUTH-05")
def test_a_reset_right_after_login_kills_the_token_at_once(
    client: TestClient, lctx: AppContext
) -> None:
    for i in range(10):  # back to back, no sleeping: no clock rounding to hide behind
        token = login(client, "ann", PASSWORD if i == 0 else f"password number {i - 1} here")
        assert client.get("/auth/me", headers=bearer(token)).status_code == 200
        _reset_password(lctx, "ann", f"password number {i} here")
        assert client.get("/auth/me", headers=bearer(token)).status_code == 401


@pytest.mark.req("AUTH-05")
def test_a_recreated_user_name_does_not_revive_old_tokens(
    client: TestClient, lctx: AppContext
) -> None:
    token = login(client, "ann")
    with lctx.write_session() as session:
        user = users_db.get_user_by_username(session, "ann")
        assert user is not None
        session.delete(user)
    add_user(lctx, "Ann", [ROLE_READER])  # same name, same password, same roles
    assert client.get("/auth/me", headers=bearer(token)).status_code == 401
    assert client.get("/auth/me", headers=bearer(login(client, "ann"))).status_code == 200


@pytest.mark.req("AUTH-11")
def test_self_service_password_change_does_not_overwrite_a_racing_admin_reset(
    client: TestClient, lctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An admin reset that lands while the caller's current password is being checked must
    not be lost: the change holds the user row until it is done, so the reset applies after."""
    import threading

    token = login(client, "ann")
    reset = threading.Thread(target=lambda: _reset_password(lctx, "ann", "the admin's reset pw"))
    real = password_api.verify_password
    started: list[bool] = []

    def verify_then_race(password: str, hashed: str) -> bool:
        ok = real(password, hashed)
        if not started:
            started.append(True)
            reset.start()
            time.sleep(0.8)  # long enough for the reset to commit if nothing stops it
        return ok

    monkeypatch.setattr(password_api, "verify_password", verify_then_race)
    r = client.post(
        "/auth/me/password",
        json={"current_password": PASSWORD, "new_password": "the user's own new pw"},
        headers=bearer(token),
    )
    assert r.status_code == 204
    reset.join(30)
    assert not reset.is_alive()
    monkeypatch.undo()
    # The admin's reset came last and is the password now; the user's change did not undo it.
    assert (
        post_token(
            client, grant_type="password", username="ann", password="the user's own new pw"
        ).status_code
        == 400
    )
    assert login(client, "ann", "the admin's reset pw")

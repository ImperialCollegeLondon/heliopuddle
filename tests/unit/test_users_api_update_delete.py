"""Users API updates and deletion (AUTH-13)."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from heliopuddle.server.auth.roles import ROLE_ADMIN, ROLE_READER
from heliopuddle.server.context import AppContext
from heliopuddle.server.models import users as users_db
from tests.local_auth import PASSWORD, bearer, issue_token, login
from tests.unit import _users_api_helpers as _fixtures
from tests.unit._users_api_helpers import body

admin = _fixtures.admin
client = _fixtures.client
lctx = _fixtures.lctx


NEW_PASSWORD = "another long password"


@pytest.mark.req("AUTH-13")
def test_usernames_with_special_characters_round_trip(
    client: TestClient, admin: dict[str, str]
) -> None:
    name = "o.brien+test@example.org"
    assert client.post("/users", json={"username": name, "password": PASSWORD}, headers=admin)
    assert (
        body(client.get("/users/o.brien%2Btest%40example.org", headers=admin))["username"] == name
    )
    assert client.delete("/users/O.Brien%2BTest%40Example.org", headers=admin).status_code == 204


@pytest.mark.req("AUTH-13")
def test_patch_roles_takes_effect_on_the_next_request(
    client: TestClient, admin: dict[str, str]
) -> None:
    token = bearer(login(client, "reader"))
    assert client.get("/users", headers=token).status_code == 403
    r = client.patch("/users/READER", json={"roles": [ROLE_ADMIN]}, headers=admin)
    assert r.status_code == 200
    assert r.json()["roles"] == [ROLE_ADMIN]
    assert client.get("/users", headers=token).status_code == 200  # same token


@pytest.mark.req("AUTH-13")
def test_patch_disable_kills_tokens_and_enable_restores(
    client: TestClient, admin: dict[str, str]
) -> None:
    token = bearer(login(client, "reader"))
    assert client.get("/auth/me", headers=token).status_code == 200
    r = client.patch("/users/reader", json={"is_active": False}, headers=admin)
    assert r.json()["is_active"] is False
    assert client.get("/auth/me", headers=token).status_code == 401
    client.patch("/users/reader", json={"is_active": True}, headers=admin)
    assert client.get("/auth/me", headers=token).status_code == 200


@pytest.mark.req("AUTH-13")
def test_patch_password_invalidates_old_tokens(
    client: TestClient, admin: dict[str, str], lctx: AppContext
) -> None:
    old = bearer(issue_token(lctx, "reader", [], 3600, now=time.time() - 30))
    with lctx.sessionmaker() as session:
        before = users_db.get_user_by_username(session, "reader")
        assert before is not None
        changed_before, updated_before = before.credentials_changed_at, before.updated_at
    r = client.patch("/users/reader", json={"password": NEW_PASSWORD}, headers=admin)
    assert r.status_code == 200
    assert NEW_PASSWORD not in r.text
    assert client.get("/auth/me", headers=old).status_code == 401
    assert login(client, "reader", NEW_PASSWORD)
    with lctx.sessionmaker() as session:
        after = users_db.get_user_by_username(session, "reader")
        assert after is not None
        assert after.credentials_changed_at > changed_before
        assert after.updated_at > updated_before
    bad = client.post(
        "/auth/token",
        data={"grant_type": "password", "username": "reader", "password": PASSWORD},
    )
    assert bad.status_code == 400


@pytest.mark.req("AUTH-13")
def test_patch_without_password_keeps_tokens_and_empty_patch_is_a_noop(
    client: TestClient, admin: dict[str, str]
) -> None:
    old = bearer(login(client, "reader"))
    assert client.patch("/users/reader", json={}, headers=admin).status_code == 200
    assert client.patch("/users/reader", json={"roles": []}, headers=admin).status_code == 200
    assert client.get("/auth/me", headers=old).status_code == 200


@pytest.mark.req("AUTH-13")
def test_patch_validation_and_unknown_user(client: TestClient, admin: dict[str, str]) -> None:
    assert (
        client.patch("/users/reader", json={"password": "short"}, headers=admin).status_code == 422
    )
    assert client.patch("/users/reader", json={"roles": ["nope"]}, headers=admin).status_code == 422
    assert client.patch("/users/ghost", json={"roles": []}, headers=admin).status_code == 404


@pytest.mark.req("AUTH-13")
def test_delete_returns_204_and_removes_access(client: TestClient, admin: dict[str, str]) -> None:
    token = bearer(login(client, "reader"))
    r = client.delete("/users/READER", headers=admin)
    assert r.status_code == 204
    assert r.content == b""
    assert client.get("/users/reader", headers=admin).status_code == 404
    assert client.get("/auth/me", headers=token).status_code == 401
    assert client.delete("/users/reader", headers=admin).status_code == 404


@pytest.mark.req("AUTH-13")
def test_the_last_active_admin_cannot_be_deleted_disabled_or_demoted(
    client: TestClient, admin: dict[str, str]
) -> None:
    for call in (
        lambda: client.delete("/users/root", headers=admin),
        lambda: client.patch("/users/root", json={"is_active": False}, headers=admin),
        lambda: client.patch("/users/root", json={"roles": [ROLE_READER]}, headers=admin),
        lambda: client.patch("/users/root", json={"roles": []}, headers=admin),
    ):
        r = call()
        assert r.status_code == 409, r.text
        assert "last active" in r.json()["detail"]
    assert body(client.get("/users/root", headers=admin))["is_active"] is True
    # harmless edits on the last admin are fine
    ok = client.patch(
        "/users/root",
        json={"roles": [ROLE_ADMIN, ROLE_READER], "password": NEW_PASSWORD},
        headers=admin,
    )
    assert ok.status_code == 200

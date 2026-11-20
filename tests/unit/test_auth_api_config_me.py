"""The auth config, identity and password change endpoints (AUTH-04, AUTH-11)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from heliopuddle.server.api import create_api
from heliopuddle.server.auth.roles import ROLE_ADMIN, ROLE_READER, ROLE_WRITER
from heliopuddle.server.context import AppContext
from tests.local_auth import PASSWORD, add_user, bearer, issue_token, local_context, login
from tests.unit import _auth_api_helpers as _fixtures
from tests.unit._auth_api_helpers import password_form, post_token

client = _fixtures.client
lctx = _fixtures.lctx


@pytest.mark.req("AUTH-11")
def test_config_is_public_and_gives_the_absolute_token_url(client: TestClient) -> None:
    r = client.get("/auth/config")  # no credentials
    assert r.status_code == 200
    assert r.json() == {
        "mode": "local",
        "token_url": "http://testserver/auth/token",
        "authorization_url": None,
    }
    assert set(r.text) and "secret" not in r.text.lower()


@pytest.mark.req("AUTH-11")
def test_config_token_url_includes_the_mount_prefix(lctx: AppContext) -> None:
    root = FastAPI()
    root.mount("/api/v1", create_api(lctx))
    with TestClient(root) as c:
        r = c.get("/api/v1/auth/config")
        assert r.json()["token_url"] == "http://testserver/api/v1/auth/token"
        assert c.post("/api/v1/auth/token", data=password_form("x", "y")).status_code == 400


@pytest.mark.req("AUTH-11")
def test_config_header_and_oidc_modes(tmp_path: Path, config_dir: Path, migrated_db: Path) -> None:
    ctx = local_context(tmp_path, config_dir, migrated_db, auth_mode="header")
    with TestClient(create_api(ctx)) as c:
        assert c.get("/auth/config").json() == {
            "mode": "header",
            "token_url": None,
            "authorization_url": None,
        }
    ctx.dispose()

    ctx = local_context(
        tmp_path / "o",
        config_dir,
        migrated_db,
        auth_mode="oidc",
        oidc_issuer="https://idp.example/realms/r",
        oidc_client_id="hp",
        oidc_client_secret="s",
    )

    class Provider:
        token_url = "https://idp.example/realms/r/protocol/openid-connect/token"
        authorization_url = "https://idp.example/realms/r/protocol/openid-connect/auth"

    with TestClient(create_api(ctx)) as c:
        body = c.get("/auth/config").json()  # discovery not available: URLs unknown
        assert body == {"mode": "oidc", "token_url": None, "authorization_url": None}
        ctx.oidc = Provider()  # type: ignore[assignment]
        body = c.get("/auth/config").json()
        assert body["mode"] == "oidc"
        assert body["token_url"] == Provider.token_url
        assert body["authorization_url"] == Provider.authorization_url
    ctx.dispose()


@pytest.mark.req("AUTH-11")
def test_me_needs_a_token_and_returns_username_roles_mode(
    client: TestClient, lctx: AppContext
) -> None:
    r = client.get("/auth/me")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"
    add_user(lctx, "wes", [ROLE_WRITER, f"{ROLE_READER}:demo"])
    token = login(client, "wes")
    r = client.get("/auth/me", headers=bearer(token))
    assert r.json() == {
        "username": "wes",
        "roles": [ROLE_WRITER, f"{ROLE_READER}:demo"],
        "mode": "local",
    }
    assert "password" not in r.text


@pytest.mark.req("AUTH-11")
def test_me_in_header_mode(tmp_path: Path, config_dir: Path, migrated_db: Path) -> None:
    ctx = local_context(tmp_path, config_dir, migrated_db, auth_mode="header")
    with TestClient(create_api(ctx)) as c:
        r = c.get("/auth/me", headers={"X-Forwarded-User": "hank"})
        assert r.json() == {"username": "hank", "roles": [ROLE_ADMIN], "mode": "header"}
    ctx.dispose()


@pytest.mark.req("AUTH-11")
def test_change_own_password(client: TestClient, lctx: AppContext) -> None:
    old = issue_token(lctx, "Ann", [], 3600, now=time.time() - 30)
    token = login(client, "ann")
    new = "a much better password"
    r = client.post(
        "/auth/me/password",
        json={"current_password": PASSWORD, "new_password": new},
        headers=bearer(token),
    )
    assert r.status_code == 204
    assert r.content == b""
    assert client.get("/auth/me", headers=bearer(old)).status_code == 401  # older token
    assert post_token(client, **password_form("ann", PASSWORD)).status_code == 400
    again = login(client, "ann", new)
    assert client.get("/auth/me", headers=bearer(again)).status_code == 200


@pytest.mark.req("AUTH-11")
def test_change_password_errors(client: TestClient, lctx: AppContext) -> None:
    url = "/auth/me/password"
    good = {"current_password": PASSWORD, "new_password": "another long password"}
    assert client.post(url, json=good).status_code == 401
    token = login(client, "ann")
    wrong = {**good, "current_password": "not my password"}
    r = client.post(url, json=wrong, headers=bearer(token))
    assert r.status_code == 400
    assert "detail" in r.json()
    short = {**good, "new_password": "short"}
    assert client.post(url, json=short, headers=bearer(token)).status_code == 422
    assert client.post(url, json={}, headers=bearer(token)).status_code == 422
    # nothing changed
    assert post_token(client, **password_form("ann", PASSWORD)).status_code == 200

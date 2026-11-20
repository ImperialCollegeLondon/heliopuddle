"""Users API authorization and creation (AUTH-13)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from heliopuddle.server.api import create_api
from heliopuddle.server.auth.passwords import verify_password
from heliopuddle.server.auth.roles import ROLE_ADMIN, ROLE_READER, ROLE_WRITER
from heliopuddle.server.context import AppContext
from heliopuddle.server.models import users as users_db
from tests.local_auth import PASSWORD, add_user, bearer, local_context, login
from tests.unit import _users_api_helpers as _fixtures
from tests.unit._users_api_helpers import body

admin = _fixtures.admin
client = _fixtures.client
lctx = _fixtures.lctx


OIDC = {
    "oidc_issuer": "https://idp.example/realms/r",
    "oidc_client_id": "hp",
    "oidc_client_secret": "s",
    "oidc_introspect": "off",
}

ROUTES = [
    ("GET", "/users"),
    ("POST", "/users"),
    ("GET", "/users/root"),
    ("PATCH", "/users/root"),
    ("DELETE", "/users/root"),
]


@pytest.mark.req("AUTH-13")
@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_every_route_needs_global_hp_admin(
    client: TestClient, admin: dict[str, str], method: str, path: str
) -> None:
    payload = {"username": "x", "password": PASSWORD} if method == "POST" else {}
    anon = client.request(method, path, json=payload)
    assert anon.status_code == 401
    assert anon.headers["www-authenticate"] == "Bearer"
    reader = client.request(method, path, json=payload, headers=bearer(login(client, "reader")))
    assert reader.status_code == 403
    assert reader.json()["required_roles"] == [ROLE_ADMIN]
    assert client.request(method, path, json=payload, headers=admin).status_code != 403


@pytest.mark.req("AUTH-13")
def test_a_project_scoped_or_non_admin_role_is_not_enough(
    client: TestClient, lctx: AppContext
) -> None:
    add_user(lctx, "w", [ROLE_WRITER, "hp-operator:demo"])
    assert client.get("/users", headers=bearer(login(client, "w"))).status_code == 403


@pytest.mark.req("AUTH-13")
def test_users_are_404_for_an_admin_in_header_mode(
    tmp_path: Path, config_dir: Path, migrated_db: Path
) -> None:
    """Header mode trusts the proxy and gives every caller ``hp-admin``; users live elsewhere."""
    ctx = local_context(tmp_path, config_dir, migrated_db, auth_mode="header")
    with TestClient(create_api(ctx)) as c:
        for method, path in ROUTES:
            r = c.request(method, path, json={"username": "x", "password": PASSWORD})
            assert r.status_code == 404, (method, path)
            assert "local" in r.json()["detail"]
    ctx.dispose()


@pytest.mark.req("AUTH-13")
def test_users_in_oidc_mode_401_then_403_then_404(
    tmp_path: Path, config_dir: Path, migrated_db: Path
) -> None:
    """The order is: authenticate (401), authorize (403), then the local-mode check (404).

    So an unauthenticated caller learns nothing about the mode; only a verified admin is
    told that users are managed elsewhere (this is what the CLI reports).
    """
    from heliopuddle.server.api import deps
    from heliopuddle.server.auth.principal import Principal

    ctx = local_context(tmp_path, config_dir, migrated_db, auth_mode="oidc", **OIDC)
    app = create_api(ctx)
    with TestClient(app) as c:
        for method, path in ROUTES:  # no token at all
            r = c.request(method, path, json={"username": "x", "password": PASSWORD})
            assert r.status_code == 401, (method, path)
        roles: tuple[str, ...] = (ROLE_READER,)
        app.dependency_overrides[deps.get_principal] = lambda: Principal(
            "root", roles, "oidc", "tok"
        )
        for method, path in ROUTES:
            r = c.request(method, path, json={"username": "x", "password": PASSWORD})
            assert r.status_code == 403, (method, path)
        roles = (ROLE_ADMIN,)
        app.dependency_overrides[deps.get_principal] = lambda: Principal(
            "root", roles, "oidc", "tok"
        )
        for method, path in ROUTES:
            r = c.request(method, path, json={"username": "x", "password": PASSWORD})
            assert r.status_code == 404, (method, path)
    ctx.dispose()


@pytest.mark.req("AUTH-13")
def test_create_returns_201_without_hash_and_sets_times(
    client: TestClient, admin: dict[str, str], lctx: AppContext
) -> None:
    r = client.post(
        "/users",
        json={"username": "Bob", "password": PASSWORD, "roles": [ROLE_READER, "hp-writer:demo"]},
        headers=admin,
    )
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["username"] == "Bob"
    assert created["roles"] == [ROLE_READER, "hp-writer:demo"]
    assert created["is_active"] is True
    assert created["created_at"] and created["updated_at"]
    assert created["last_login_at"] is None
    assert not {"password", "password_hash"} & set(created)
    assert PASSWORD not in r.text
    with lctx.sessionmaker() as session:
        user = users_db.get_user_by_username(session, "bob")
        assert user is not None
        assert verify_password(PASSWORD, user.password_hash)
        assert user.password_hash not in r.text
    # the new user can log in
    assert login(client, "BOB")


@pytest.mark.req("AUTH-13")
def test_create_defaults_and_inactive(client: TestClient, admin: dict[str, str]) -> None:
    r = client.post(
        "/users", json={"username": "off", "password": PASSWORD, "is_active": False}, headers=admin
    )
    assert r.status_code == 201
    assert r.json()["roles"] == []
    assert r.json()["is_active"] is False
    login_r = client.post(
        "/auth/token",
        data={"grant_type": "password", "username": "off", "password": PASSWORD},
    )
    assert login_r.status_code == 400


@pytest.mark.req("AUTH-13")
def test_duplicate_username_is_409_ignoring_case(client: TestClient, admin: dict[str, str]) -> None:
    for name in ("root", "ROOT", "Root"):
        r = client.post("/users", json={"username": name, "password": PASSWORD}, headers=admin)
        assert r.status_code == 409, name
        assert "already exists" in r.json()["detail"]


@pytest.mark.req("AUTH-13")
@pytest.mark.parametrize(
    "payload",
    [
        {"username": "x", "password": "short"},
        {"username": "x", "password": PASSWORD, "roles": ["hp-boss"]},
        {"username": "x", "password": PASSWORD, "roles": ["hp-admin:demo"]},
        {"username": "", "password": PASSWORD},
        {"password": PASSWORD},
    ],
)
def test_invalid_create_is_422(
    client: TestClient, admin: dict[str, str], payload: dict[str, object]
) -> None:
    r = client.post("/users", json=payload, headers=admin)
    assert r.status_code == 422
    assert PASSWORD not in r.text


@pytest.mark.req("AUTH-13")
def test_list_and_get(client: TestClient, admin: dict[str, str]) -> None:
    users = body(client.get("/users", headers=admin))
    assert [u["username"] for u in users] == ["reader", "Root"]
    for u in users:
        assert "password_hash" not in u
    one = body(client.get("/users/ROOT", headers=admin))
    assert one["username"] == "Root"
    assert one["roles"] == [ROLE_ADMIN]
    missing = client.get("/users/nobody", headers=admin)
    assert missing.status_code == 404

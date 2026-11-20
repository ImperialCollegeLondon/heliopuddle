"""Global role enforcement on every API route (AUTH-07)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from heliopuddle.server.auth.roles import (
    ROLE_ADMIN,
    ROLE_READER,
)
from heliopuddle.server.context import AppContext
from tests.unit import _auth_enforcement_helpers as _fixtures
from tests.unit._auth_enforcement_helpers import (
    ENDPOINTS,
    PROJECT_ROLE_CASES,
    Act,
    Endpoint,
    World,
    _call,
    _denied,
    _roles_allowed,
)

act_as = _fixtures.act_as
app = _fixtures.app
client = _fixtures.client
config_dir = _fixtures.config_dir
world = _fixtures.world


IDS = [e.name for e in ENDPOINTS]


@pytest.mark.req("AUTH-07")
@pytest.mark.parametrize("endpoint", ENDPOINTS, ids=IDS)
def test_missing_credentials_are_401_with_challenge(
    client: TestClient, world: World, endpoint: Endpoint, ctx: AppContext
) -> None:
    ctx.settings = ctx.settings.model_copy(update={"auth_mode": "local"})
    client.app.dependency_overrides.clear()  # type: ignore[attr-defined]
    r = _call(client, endpoint, world)
    assert r.status_code == 401
    assert r.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.req("AUTH-07")
@pytest.mark.parametrize("endpoint", ENDPOINTS, ids=IDS)
@pytest.mark.parametrize("role", [*PROJECT_ROLE_CASES, ROLE_ADMIN])
def test_global_role_matrix(
    client: TestClient, act_as: Act, world: World, endpoint: Endpoint, role: str
) -> None:
    assert endpoint.needed is not None
    act_as(role)
    r = _call(client, endpoint, world)
    if role in _roles_allowed(endpoint.needed):
        assert not _denied(r), (r.status_code, r.text)
    else:
        assert r.status_code == 403, (r.status_code, r.text)
        body = r.json()
        assert body["required_roles"] == (
            [ROLE_ADMIN]
            if endpoint.needed == ROLE_ADMIN
            else [endpoint.needed, f"{endpoint.needed}:{endpoint.project}", ROLE_ADMIN]
        )
        assert endpoint.needed in body["detail"]


@pytest.mark.req("AUTH-07")
@pytest.mark.parametrize("endpoint", ENDPOINTS, ids=IDS)
def test_caller_with_no_roles_is_denied_everywhere(
    client: TestClient, act_as: Act, world: World, endpoint: Endpoint
) -> None:
    act_as()
    r = _call(client, endpoint, world)
    assert r.status_code == 404  # no role at all on the project: existence is hidden
    assert r.json() == {"detail": "not found"}


@pytest.mark.req("AUTH-07")
def test_forbidden_body_matches_the_schema(client: TestClient, act_as: Act, world: World) -> None:
    from heliopuddle.server.schemas.auth import Forbidden

    act_as(ROLE_READER)
    r = client.delete(f"/files/{world.demo_file}")
    assert r.status_code == 403
    assert Forbidden.model_validate(r.json()).required_roles == [
        "hp-deleter",
        "hp-deleter:demo",
        "hp-admin",
    ]


@pytest.mark.req("AUTH-07")
def test_denied_write_changes_nothing(
    client: TestClient, act_as: Act, world: World, ctx: AppContext
) -> None:
    act_as(ROLE_READER)
    assert client.delete(f"/files/{world.demo_file}").status_code == 403
    assert client.patch(f"/files/{world.demo_file}/metadata", json={"a": 1}).status_code == 403
    act_as(ROLE_READER)
    got = client.get(f"/files/{world.demo_file}").json()
    assert got["deleted_at"] is None
    assert not got["metadata"]


@pytest.mark.req("AUTH-07")
def test_unknown_role_strings_grant_nothing(client: TestClient, act_as: Act, world: World) -> None:
    act_as("offline_access", "hp-owner", "hp-admin:demo")
    assert client.get("/projects/demo").status_code == 404
    assert client.get("/projects").json() == []

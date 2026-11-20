"""Roles on a project removed from the config (AUTH-07)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from heliopuddle.server.auth.roles import (
    ROLE_ADMIN,
    ROLE_DELETER,
    ROLE_OPERATOR,
    ROLE_READER,
    ROLE_WRITER,
)
from heliopuddle.server.context import AppContext
from tests.unit import _auth_enforcement_helpers as _fixtures
from tests.unit._auth_enforcement_helpers import STORAGE_ROLE, Act, World

act_as = _fixtures.act_as
app = _fixtures.app
client = _fixtures.client
config_dir = _fixtures.config_dir
world = _fixtures.world


@pytest.fixture
def removed(ctx: AppContext, world: World) -> World:
    """``other`` is dropped from the config; its rows stay in the DB."""
    ctx.projects.pop("other")
    return world


@pytest.mark.req("AUTH-08")
@pytest.mark.parametrize(
    ("role", "method", "path"),
    [
        (ROLE_READER, "GET", "/files/{other_file}"),
        (ROLE_READER, "GET", "/series/{other_series}/versions"),
        (ROLE_READER, "GET", "/projects/other/files"),
        (ROLE_WRITER, "PATCH", "/files/{other_file}/metadata"),
        (ROLE_WRITER, "POST", "/uploads/{other_token}/renew"),
        (ROLE_DELETER, "DELETE", "/files/{other_file}"),
        (ROLE_OPERATOR, "POST", "/projects/other/runs"),
        (STORAGE_ROLE, "GET", "/projects/other/storage-locations/main"),
        (STORAGE_ROLE, "GET", "/files/{other_file}"),
    ],
)
def test_scoped_roles_grant_nothing_on_a_removed_project(
    client: TestClient, act_as: Act, removed: World, role: str, method: str, path: str
) -> None:
    act_as(f"{role}:other")
    url = path.format(**removed.__dict__)
    r = client.request(method, url, json={"x": 1} if method in {"PATCH", "POST"} else None)
    assert r.status_code == 404, (method, url, r.text)
    assert r.json() == {"detail": "not found"}


@pytest.mark.req("AUTH-08")
def test_global_roles_still_reach_a_removed_projects_rows(
    client: TestClient, act_as: Act, removed: World
) -> None:
    act_as(ROLE_READER)
    assert client.get(f"/files/{removed.other_file}").status_code == 200
    act_as(ROLE_ADMIN)
    assert client.get(f"/series/{removed.other_series}/versions").status_code == 200


@pytest.mark.req("AUTH-08")
def test_removing_one_project_leaves_scoped_roles_on_the_others_working(
    client: TestClient, act_as: Act, removed: World
) -> None:
    act_as(f"{ROLE_READER}:demo", f"{ROLE_READER}:other")
    assert client.get(f"/files/{removed.demo_file}").status_code == 200
    assert client.get(f"/files/{removed.other_file}").status_code == 404
    assert [p["name"] for p in client.get("/projects").json()] == ["demo"]

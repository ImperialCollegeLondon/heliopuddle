"""Project-scoped role enforcement (AUTH-07, AUTH-08)."""

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
from tests.unit import _auth_enforcement_helpers as _fixtures
from tests.unit._auth_enforcement_helpers import (
    ENDPOINTS,
    PROJECT_ROLE_CASES,
    STORAGE_ROLE,
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


@pytest.mark.req("AUTH-08")
@pytest.mark.parametrize(
    "endpoint", [e for e in ENDPOINTS if e.needed != ROLE_ADMIN], ids=lambda e: e.name
)
@pytest.mark.parametrize("role", PROJECT_ROLE_CASES)
def test_scoped_role_works_on_its_project_only(
    client: TestClient, act_as: Act, world: World, endpoint: Endpoint, role: str
) -> None:
    assert endpoint.needed is not None
    act_as(f"{role}:demo")
    own = _call(client, endpoint, world, "demo")
    if role in _roles_allowed(endpoint.needed):
        assert not _denied(own), (own.status_code, own.text)
    else:
        assert own.status_code == 403, (own.status_code, own.text)
        assert own.json()["required_roles"] == [
            endpoint.needed,
            f"{endpoint.needed}:demo",
            ROLE_ADMIN,
        ]
    r = _call(client, endpoint, world, "other")
    assert r.status_code == 404  # holds no role at all on `other`
    assert r.json() == {"detail": "not found"}


@pytest.mark.req("AUTH-08")
def test_scoped_role_for_the_wrong_capability_is_403_on_its_project(
    client: TestClient, act_as: Act, world: World
) -> None:
    act_as("hp-writer:demo")
    r = client.delete(f"/files/{world.demo_file}")
    assert r.status_code == 403
    assert r.json()["required_roles"] == ["hp-deleter", "hp-deleter:demo", "hp-admin"]
    assert client.delete(f"/files/{world.other_file}").status_code == 404


@pytest.mark.req("AUTH-08")
@pytest.mark.parametrize("base", [ROLE_WRITER, ROLE_DELETER, ROLE_OPERATOR, STORAGE_ROLE])
def test_scoped_capability_roles_include_read(
    client: TestClient, act_as: Act, world: World, base: str
) -> None:
    act_as(f"{base}:demo")
    assert client.get("/projects/demo").status_code == 200
    assert client.get("/projects/demo/files").status_code == 200
    assert client.get(f"/files/{world.demo_file}").status_code == 200
    assert client.get(f"/series/{world.demo_series}/versions").status_code == 200
    assert client.get("/projects/other/files").status_code == 404


@pytest.mark.req("AUTH-08")
def test_multi_project_roles(client: TestClient, act_as: Act, world: World) -> None:
    act_as("hp-reader:demo", "hp-writer:other")
    assert client.get("/projects/demo").status_code == 200
    assert client.get("/projects/other/files").status_code == 200  # writer implies read
    meta = client.patch(f"/files/{world.other_file}/metadata", json={"a": 1})
    assert meta.status_code == 200
    denied = client.patch(f"/files/{world.demo_file}/metadata", json={"a": 1})
    assert denied.status_code == 403  # reader only on demo
    assert denied.json()["required_roles"] == ["hp-writer", "hp-writer:demo", "hp-admin"]


@pytest.mark.req("AUTH-08")
def test_global_role_and_scoped_role_combine(client: TestClient, act_as: Act, world: World) -> None:
    act_as("hp-reader", "hp-deleter:demo")
    assert client.get(f"/files/{world.other_file}").status_code == 200
    assert client.delete(f"/files/{world.other_file}").status_code == 403
    assert client.delete(f"/files/{world.demo_file}").status_code == 200


@pytest.mark.req("AUTH-08")
def test_id_routes_resolve_the_project_from_the_row(
    client: TestClient, act_as: Act, world: World
) -> None:
    act_as("hp-writer:demo")
    # `other`'s rows are invisible: file, series and upload-token addressed alike.
    assert client.get(f"/files/{world.other_file}").json() == {"detail": "not found"}
    assert client.get(f"/series/{world.other_series}/versions").status_code == 404
    assert client.post(f"/uploads/{world.other_token}/renew").json() == {"detail": "not found"}
    assert client.delete(f"/uploads/{world.other_token}").status_code == 404
    assert client.post(f"/uploads/{world.other_token}/complete").status_code == 404
    # ...and still exist, and demo's are reachable.
    assert client.post(f"/uploads/{world.demo_token}/renew").status_code == 200
    act_as(ROLE_ADMIN)
    assert client.post(f"/uploads/{world.other_token}/renew").status_code == 200


@pytest.mark.req("AUTH-08")
def test_missing_id_looks_like_a_forbidden_one_to_an_unprivileged_caller(
    client: TestClient, act_as: Act, world: World
) -> None:
    act_as("hp-writer:demo")
    missing = client.get("/files/999999")
    hidden = client.get(f"/files/{world.other_file}")
    assert (missing.status_code, missing.json()) == (hidden.status_code, hidden.json())
    assert client.get("/series/999999/versions").json() == hidden.json()
    assert client.post("/uploads/no-such-token/renew").json() == hidden.json()
    assert client.get("/files/not-a-number").json() == hidden.json()
    assert client.get(f"/files/{2**70}").json() == hidden.json()


@pytest.mark.req("AUTH-08")
def test_missing_id_reaches_the_routes_own_404_for_a_global_role(
    client: TestClient, act_as: Act, world: World
) -> None:
    act_as(ROLE_READER)
    r = client.get("/files/999999")
    assert r.status_code == 404
    assert r.json() == {"detail": "file 999999 not found"}
    act_as(ROLE_READER)
    assert client.delete("/files/999999").json() == {"detail": "not found"}  # not a deleter
    act_as(ROLE_DELETER)
    assert client.delete("/files/999999").json() == {"detail": "file 999999 not found"}
    act_as(ROLE_ADMIN)
    assert client.get("/projects/nope").json() == {"detail": "project 'nope' not found"}


@pytest.mark.req("AUTH-08")
def test_unknown_project_is_404_for_everyone_without_a_global_role(
    client: TestClient, act_as: Act, world: World
) -> None:
    act_as("hp-reader:demo")
    assert client.get("/projects/nope/files").json() == {"detail": "not found"}
    assert client.get("/projects/other/files").json() == {"detail": "not found"}


@pytest.mark.req("AUTH-08")
def test_list_projects_is_filtered_to_projects_with_any_role(
    client: TestClient, act_as: Act, world: World
) -> None:
    def names() -> list[str]:
        return sorted(p["name"] for p in client.get("/projects").json())

    act_as(ROLE_ADMIN)
    assert names() == ["demo", "other"]
    act_as(ROLE_READER)
    assert names() == ["demo", "other"]
    act_as("hp-operator:other")
    assert names() == ["other"]
    act_as(f"{STORAGE_ROLE}:demo")
    assert names() == ["demo"]
    act_as("hp-reader:demo", "hp-deleter:other")
    assert names() == ["demo", "other"]
    act_as("hp-reader:retired")  # a scoped role for a project that is not configured
    assert names() == []
    act_as()
    r = client.get("/projects")
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.req("AUTH-08")
def test_scoped_role_for_an_unconfigured_project_grants_nothing(
    client: TestClient, act_as: Act, world: World
) -> None:
    act_as("hp-admin", "hp-writer:retired")
    assert client.get("/projects/demo").status_code == 200
    act_as("hp-writer:retired")
    assert client.get("/projects/demo").status_code == 404


@pytest.mark.req("AUTH-08")
def test_identity_still_flows_to_created_by(client: TestClient, act_as: Act, world: World) -> None:
    act_as("hp-writer:demo", username="carol")
    r = client.patch(f"/files/{world.demo_file}/metadata", json={"k": "v"})
    assert r.status_code == 200
    assert r.json()["updated_by"] == "carol"

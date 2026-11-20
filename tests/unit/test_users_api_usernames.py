"""Users API username rules (AUTH-13)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.local_auth import PASSWORD
from tests.unit import _users_api_helpers as _fixtures
from tests.unit._users_api_helpers import body

admin = _fixtures.admin
client = _fixtures.client
lctx = _fixtures.lctx


@pytest.mark.req("AUTH-13")
@pytest.mark.parametrize(
    "name",
    [
        "a/b",
        "a b",
        " ann",
        "ann ",
        "Älice",
        "ünïcode",
        "ann\u200b",
        ".hidden",
        "-dash",
        "_x",
        "a\\b",
        "a?b",
        "a#b",
        "a%b",
        "",
        "x" * 256,
        "..",
        "ann\n",
    ],
)
def test_usernames_outside_the_alphabet_are_rejected(
    client: TestClient, admin: dict[str, str], name: str
) -> None:
    r = client.post("/users", json={"username": name, "password": PASSWORD}, headers=admin)
    assert r.status_code == 422, name
    assert PASSWORD not in r.text


@pytest.mark.req("AUTH-13")
@pytest.mark.parametrize(
    "name", ["Ann", "a", "a.b-c_d", "a+b", "ann@example.org", "x" * 255, "9lives"]
)
def test_every_accepted_username_is_addressable_through_the_api(
    client: TestClient, admin: dict[str, str], name: str
) -> None:
    from urllib.parse import quote

    assert (
        client.post(
            "/users", json={"username": name, "password": PASSWORD}, headers=admin
        ).status_code
        == 201
    )
    url = f"/users/{quote(name, safe='')}"
    assert body(client.get(url, headers=admin))["username"] == name
    assert client.get(url.upper() if name.isascii() else url, headers=admin).status_code in (
        200,
        404,
    )
    assert client.patch(url, json={"is_active": False}, headers=admin).status_code == 200
    assert client.delete(url, headers=admin).status_code == 204
    assert client.get(url, headers=admin).status_code == 404


@pytest.mark.req("AUTH-13")
def test_names_differing_only_in_case_are_one_user(
    client: TestClient, admin: dict[str, str]
) -> None:
    assert (
        client.post(
            "/users", json={"username": "Ann", "password": PASSWORD}, headers=admin
        ).status_code
        == 201
    )
    r = client.post("/users", json={"username": "aNN", "password": PASSWORD}, headers=admin)
    assert r.status_code == 409


@pytest.mark.req("AUTH-13")
def test_login_with_a_non_ascii_name_is_just_invalid_grant(client: TestClient) -> None:
    r = client.post(
        "/auth/token",
        data={"grant_type": "password", "username": "R\u00e9ader", "password": PASSWORD},
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_grant"

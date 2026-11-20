"""Client password change and user management calls."""

from __future__ import annotations

import json

import httpx
import pytest

from heliopuddle.client import (
    HelioPuddleError,
    NotFoundError,
    PermissionDeniedError,
)
from tests.unit._client_auth_helpers import _api, _user


@pytest.mark.req("AUTH-15")
def test_change_password_posts_both_passwords() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    with _api(handler) as c:
        assert c.change_password("old-old-old-old", "new-new-new-new") is None
    (req,) = seen
    assert (req.method, req.url.path) == ("POST", "/api/v1/auth/me/password")
    assert json.loads(req.content) == {
        "current_password": "old-old-old-old",
        "new_password": "new-new-new-new",
    }


@pytest.mark.req("AUTH-15")
def test_user_management_methods() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "DELETE":
            return httpx.Response(204)
        if request.method == "GET" and request.url.path.endswith("/users"):
            return httpx.Response(200, json=[_user("a"), _user("b")])
        if request.method == "POST":
            return httpx.Response(201, json=_user("new user", roles=["hp-reader"]))
        return httpx.Response(200, json=_user("a/b"))

    with _api(handler) as c:
        users = c.list_users()
        assert [u.username for u in users] == ["a", "b"]
        assert c.get_user("a/b").username == "a/b"
        created = c.create_user("new user", "long-enough-pw!", roles=["hp-reader"])
        assert created.roles == ["hp-reader"]
        c.create_user("plain", "long-enough-pw!")
        c.update_user("a/b", roles=["hp-admin"])
        c.update_user("a/b", password="another-long-pw!", is_active=False)
        assert c.update_user("a/b").username == "a/b"
        assert c.delete_user("a/b") is None

    calls = [(r.method, r.url.raw_path.decode()) for r in seen]
    assert calls == [
        ("GET", "/api/v1/users"),
        ("GET", "/api/v1/users/a%2Fb"),
        ("POST", "/api/v1/users"),
        ("POST", "/api/v1/users"),
        ("PATCH", "/api/v1/users/a%2Fb"),
        ("PATCH", "/api/v1/users/a%2Fb"),
        ("PATCH", "/api/v1/users/a%2Fb"),
        ("DELETE", "/api/v1/users/a%2Fb"),
    ]
    bodies = [json.loads(r.content) if r.content else None for r in seen]
    assert bodies[2] == {
        "username": "new user",
        "password": "long-enough-pw!",
        "roles": ["hp-reader"],
        "is_active": True,
    }
    assert bodies[3] == {"username": "plain", "password": "long-enough-pw!", "is_active": True}
    assert bodies[4] == {"roles": ["hp-admin"]}
    assert bodies[5] == {"password": "another-long-pw!", "is_active": False}
    assert bodies[6] == {}


@pytest.mark.req("AUTH-15")
def test_user_errors_map_to_exceptions() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(409, json={"detail": "username taken"})
        if request.method == "DELETE":
            return httpx.Response(
                403, json={"detail": "admin only", "required_roles": ["hp-admin"]}
            )
        return httpx.Response(404, json={"detail": "no such user"})

    with _api(handler) as c:
        with pytest.raises(HelioPuddleError) as info:
            c.create_user("x", "long-enough-pw!")
        assert info.value.status_code == 409
        with pytest.raises(PermissionDeniedError) as denied:
            c.delete_user("x")
        assert denied.value.required_roles == ["hp-admin"]
        with pytest.raises(NotFoundError):
            c.get_user("x")

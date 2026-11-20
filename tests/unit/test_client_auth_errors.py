"""Client authentication and permission errors, and identity calls."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from heliopuddle.client import (
    AuthenticationError,
    HelioPuddleClient,
    HelioPuddleError,
    NotFoundError,
    PermissionDeniedError,
)
from tests.unit._client_auth_helpers import API, IDP, Fake


@pytest.mark.req("AUTH-15")
@pytest.mark.parametrize(
    "body",
    [
        {"detail": "need one of", "required_roles": ["hp-writer", "hp-admin"]},
        {"detail": {"detail": "need one of", "required_roles": ["hp-writer", "hp-admin"]}},
    ],
)
def test_403_raises_permission_denied_with_required_roles(body: dict[str, Any]) -> None:
    fake = Fake("local", accepted=set())
    fake.api_status["/projects"] = (403, body)
    with (
        fake.client(username="alice", password="s3cret-pass-phrase") as c,
        pytest.raises(PermissionDeniedError) as info,
    ):
        c.list_projects()
    assert info.value.status_code == 403
    assert info.value.required_roles == ["hp-writer", "hp-admin"]
    assert isinstance(info.value, HelioPuddleError)
    assert fake.issued == 1  # a 403 does not trigger a refresh


@pytest.mark.req("AUTH-15")
def test_403_without_roles_gives_empty_list() -> None:
    fake = Fake("header")
    fake.api_status["/projects"] = (403, {"detail": "no"})
    with fake.client(user="a") as c, pytest.raises(PermissionDeniedError) as info:
        c.list_projects()
    assert info.value.required_roles == []


@pytest.mark.req("AUTH-15")
def test_404_stays_not_found() -> None:
    fake = Fake("header")
    fake.api_status["/projects"] = (404, {"detail": "nope"})
    with fake.client(user="a") as c, pytest.raises(NotFoundError):
        c.list_projects()


@pytest.mark.req("AUTH-15")
@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (
            400,
            {"error": "invalid_grant", "error_description": "bad credentials"},
            "bad credentials",
        ),
        (401, {"error": "invalid_client"}, "invalid_client"),
        (401, {"detail": "Invalid username or password"}, "Invalid username or password"),
        (500, {}, "500"),
    ],
)
def test_token_endpoint_failure_raises_authentication_error(
    status: int, body: dict[str, Any], expected: str
) -> None:
    fake = Fake("local")
    fake.token_error = (status, body)
    password = "do-not-leak-this-pw"
    with (
        fake.client(username="alice", password=password) as c,
        pytest.raises(AuthenticationError) as info,
    ):
        c.list_projects()
    assert expected in str(info.value)
    assert password not in str(info.value)
    assert password not in repr(info.value)


@pytest.mark.req("AUTH-15")
def test_config_endpoint_missing_with_credentials_is_authentication_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "nope"})

    http = httpx.Client(base_url=API, transport=httpx.MockTransport(handler))
    with (
        HelioPuddleClient(API, "demo", username="a", password="b" * 12, http_client=http) as c,
        pytest.raises(AuthenticationError),
    ):
        c.list_projects()


@pytest.mark.req("AUTH-15")
def test_secrets_never_in_repr() -> None:
    fake = Fake("local", accepted=set())
    with fake.client(username="alice", password="do-not-leak-this-pw") as c:
        c.list_projects()
        text = repr(c) + str(vars(c)).replace("do-not-leak-this-pw", "") + repr(c._auth)  # pyright: ignore[reportPrivateUsage]
        assert "tok1" not in text
        assert "do-not-leak-this-pw" not in repr(c)
        assert "do-not-leak-this-pw" not in repr(c._auth)  # pyright: ignore[reportPrivateUsage]
    with HelioPuddleClient(API, "demo", token="sekrit-token") as c2:
        assert "sekrit-token" not in repr(c2)
        assert "sekrit-token" not in repr(c2._auth)  # pyright: ignore[reportPrivateUsage]
    with HelioPuddleClient(API, "demo", client_id="svc", client_secret="sekrit-secret") as c3:
        assert "sekrit-secret" not in repr(c3._auth)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.req("AUTH-15")
def test_me_and_auth_config() -> None:
    fake = Fake("local", accepted=set())
    with fake.client(username="alice", password="s3cret-pass-phrase") as c:
        me = c.me()
        assert me.username == "alice"
        assert me.roles == ["hp-reader"]
        assert me.can("hp-reader")
        assert not me.can("hp-admin")
        cfg = c.auth_config()
        assert cfg.mode == "local"
        assert cfg.token_url == f"{API}/auth/token"


@pytest.mark.req("AUTH-15")
def test_auth_config_works_without_credentials() -> None:
    fake = Fake("oidc", token_url=IDP)
    with fake.client() as c:
        assert c.auth_config().mode == "oidc"
        assert "authorization" not in fake.requests[-1].headers

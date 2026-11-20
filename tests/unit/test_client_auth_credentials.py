"""Client credential options and grants against a fake API and identity provider."""

from __future__ import annotations

import io
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from heliopuddle.client import (
    AuthenticationError,
    HelioPuddleClient,
    PermissionDeniedError,
)
from tests.unit._client_auth_helpers import API, IDP, Fake, bearer, form


@pytest.mark.req("AUTH-15", "CLI-08")
def test_api_upload_body_is_replayed_after_token_refresh() -> None:
    fake = Fake()
    received: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            received.append(request.read())
            return httpx.Response(401 if len(received) == 1 else 204)
        return fake.handler(request)

    source = io.BytesIO(b"complete body")

    def content() -> Iterator[bytes]:
        source.seek(0)
        while chunk := source.read(3):
            yield chunk

    with httpx.Client(base_url=API, transport=httpx.MockTransport(handler)) as http:
        client = HelioPuddleClient(
            API, "demo", username="alice", password="password", http_client=http
        )
        assert (
            client._request("PUT", "/uploads/token/content", body_factory=content).status_code
            == 204
        )
    assert received == [b"complete body", b"complete body"]
    assert fake.issued == 2


@pytest.mark.req("AUTH-15")
def test_streamed_api_errors_are_read_and_response_closed() -> None:
    class ErrorBody(httpx.SyncByteStream):
        closed = False

        def __iter__(self) -> Iterator[bytes]:
            yield b'{"detail":"forbidden","required_roles":["hp-writer"]}'

        def close(self) -> None:
            self.closed = True

    body = ErrorBody()
    with httpx.Client(
        base_url=API,
        transport=httpx.MockTransport(lambda request: httpx.Response(403, stream=body)),
    ) as http:
        client = HelioPuddleClient(API, "demo", token="token", http_client=http)
        with pytest.raises(PermissionDeniedError) as error:
            client._request("GET", "/files/1/content", stream=True)
    assert error.value.required_roles == ["hp-writer"]
    assert body.closed


@pytest.mark.req("AUTH-15")
def test_content_fallback_does_not_hide_unrelated_permission_errors() -> None:
    fake = Fake()
    path = "/projects/demo/storage-locations/primary"
    fake.api_status[path] = (403, {"detail": "denied", "required_roles": ["hp-admin"]})
    client = fake.client(token="token")
    with pytest.raises(PermissionDeniedError):
        client._transfer_location("primary")


@pytest.mark.req("AUTH-15")
@pytest.mark.parametrize(
    "kwargs",
    [
        {"token": "t", "username": "a", "password": "b"},
        {"token": "t", "client_id": "c", "client_secret": "s"},
        {"username": "a", "password": "b", "client_secret": "s"},
        {"token": "t", "username": "a", "password": "b", "client_id": "c"},
        {"username": "a"},
        {"password": "b"},
        {"client_id": "c"},
        {"client_secret": "s"},
        {"token": "t", "client_secret": "s"},
    ],
)
def test_invalid_credential_combinations(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        HelioPuddleClient(API, "demo", **kwargs)


@pytest.mark.req("AUTH-15")
def test_existing_positional_usage_still_works() -> None:
    c = HelioPuddleClient(API, "demo", "alice")
    assert c.user == "alice"
    c.close()


@pytest.mark.req("AUTH-15")
def test_static_token_is_sent_without_any_lookup() -> None:
    fake = Fake(accepted={"abc"})
    with fake.client(token="abc") as c:
        c.list_projects()
        c.list_projects()
    assert fake.config_calls == 0
    assert not fake.token_requests
    assert [bearer(r) for r in fake.requests] == ["Bearer abc"] * 2


@pytest.mark.req("AUTH-15")
def test_static_token_401_raises_without_retry() -> None:
    fake = Fake(accepted={"good"})
    with fake.client(token="stale") as c, pytest.raises(AuthenticationError) as info:
        c.list_projects()
    assert len(fake.requests) == 1
    assert info.value.status_code == 401
    assert "stale" not in str(info.value)


@pytest.mark.req("AUTH-15")
def test_local_username_password_posts_password_grant_to_config_token_url() -> None:
    fake = Fake("local", accepted=set())
    with fake.client(username="alice", password="s3cret-pass-phrase") as c:
        assert fake.requests == []  # lazy
        c.list_projects()
    assert fake.config_calls == 1
    (tok,) = fake.token_requests
    assert tok.method == "POST"
    assert str(tok.url) == f"{API}/auth/token"
    assert form(tok) == {
        "grant_type": "password",
        "username": "alice",
        "password": "s3cret-pass-phrase",
    }
    assert bearer(fake.requests[-1]) == "Bearer tok1"


@pytest.mark.req("AUTH-15")
def test_local_token_url_falls_back_to_api_auth_token() -> None:
    fake = Fake("local", token_url=None)
    with fake.client(username="alice", password="s3cret-pass-phrase") as c:
        c.list_projects()
    assert str(fake.token_requests[0].url) == f"{API}/auth/token"


@pytest.mark.req("AUTH-15")
def test_local_mode_rejects_client_credentials() -> None:
    fake = Fake("local")
    with (
        fake.client(client_id="svc", client_secret="shh") as c,
        pytest.raises(AuthenticationError) as info,
    ):
        c.list_projects()
    assert "shh" not in str(info.value)
    assert not fake.token_requests


@pytest.mark.req("AUTH-15")
def test_oidc_password_grant_with_confidential_client() -> None:
    fake = Fake("oidc", token_url=IDP)
    with fake.client(
        username="bob", password="pw-pw-pw-pw-pw", client_id="hp", client_secret="sec"
    ) as c:
        c.list_projects()
    (tok,) = fake.token_requests
    assert str(tok.url) == IDP
    assert tok.headers["authorization"].startswith("Basic ")  # client_secret_basic
    assert form(tok) == {"grant_type": "password", "username": "bob", "password": "pw-pw-pw-pw-pw"}
    assert bearer(fake.requests[-1]) == "Bearer tok1"


@pytest.mark.req("AUTH-15")
def test_oidc_password_grant_with_public_client_id_only() -> None:
    fake = Fake("oidc", token_url=IDP)
    with fake.client(username="bob", password="pw-pw-pw-pw-pw", client_id="hp") as c:
        c.list_projects()
    # client_id (a public client) may accompany username/password
    assert form(fake.token_requests[0])["client_id"] == "hp"
    assert "authorization" not in fake.token_requests[0].headers


@pytest.mark.req("AUTH-15")
def test_oidc_client_credentials_grant() -> None:
    fake = Fake("oidc", token_url=IDP)
    with fake.client(client_id="svc", client_secret="sec") as c:
        c.list_projects()
    (tok,) = fake.token_requests
    assert str(tok.url) == IDP
    assert form(tok) == {"grant_type": "client_credentials"}
    assert tok.headers["authorization"].startswith("Basic ")
    assert bearer(fake.requests[-1]) == "Bearer tok1"


@pytest.mark.req("AUTH-15")
def test_oidc_without_token_url_is_an_authentication_error() -> None:
    fake = Fake("oidc", token_url=None)
    with fake.client(client_id="svc", client_secret="sec") as c, pytest.raises(AuthenticationError):
        c.list_projects()

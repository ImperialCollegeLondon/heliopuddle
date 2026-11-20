"""Client credentials and HTTPS origins."""

from __future__ import annotations

import pytest

from heliopuddle.client import (
    AuthenticationError,
)
from tests.unit._client_auth_helpers import IDP, SECURE_API, Fake, secure_client


@pytest.mark.req("AUTH-15")
def test_local_login_stays_on_the_https_origin_when_the_server_announces_http() -> None:
    # A TLS-terminating proxy that uvicorn does not trust makes the server think it is
    # plain http, so it announces http://... as its own token URL.
    client, seen = secure_client("http://api.example/api/v1/auth/token")
    assert client.me().username == "alice"
    posts = [r for r in seen if r.method == "POST"]
    assert [str(r.url) for r in posts] == [f"{SECURE_API}/auth/token"]
    assert all(r.url.scheme == "https" for r in seen)  # nothing at all went over http


@pytest.mark.req("AUTH-15")
@pytest.mark.parametrize(
    "announced",
    ["https://evil.example/token", "http://evil.example/token", "//evil.example/token", None],
)
def test_local_login_ignores_whatever_token_url_the_server_announces(
    announced: str | None,
) -> None:
    client, seen = secure_client(announced)
    client.me()
    assert {r.url.host for r in seen} == {"api.example"}
    assert [r.url.path for r in seen if r.method == "POST"] == ["/api/v1/auth/token"]


@pytest.mark.req("AUTH-15")
def test_oidc_refuses_to_send_credentials_to_a_plain_http_token_endpoint_from_an_https_api() -> (
    None
):
    client, seen = secure_client("http://idp.example/token", mode="oidc")
    with pytest.raises(AuthenticationError, match="refusing to send credentials"):
        client.me()
    assert not [r for r in seen if r.url.host == "idp.example"]  # nothing was sent
    assert "a-long-enough-password" not in "".join(str(r.content) for r in seen)


@pytest.mark.req("AUTH-15")
def test_oidc_over_https_end_to_end_is_allowed() -> None:
    client, seen = secure_client("https://idp.example/token", mode="oidc")
    assert client.me().username == "alice"
    assert [str(r.url) for r in seen if r.method == "POST"] == ["https://idp.example/token"]


@pytest.mark.req("AUTH-15")
def test_oidc_over_http_for_local_development_is_allowed() -> None:
    fake = Fake("oidc", token_url=IDP)
    client = fake.client(username="alice", password="a-long-enough-password")
    assert client.me().username == "alice"
    assert [str(r.url) for r in fake.token_requests] == [IDP]

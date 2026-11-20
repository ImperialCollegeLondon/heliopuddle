"""OIDC code exchange and token introspection (AUTH-09, AUTH-10)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from heliopuddle.server.auth.errors import AuthError
from heliopuddle.server.auth.oidc import OidcProvider
from tests.fake_oidc import DEFAULT_CLIENT_SECRET, FakeIssuer
from tests.unit import _auth_oidc_helpers as _fixtures
from tests.unit._auth_oidc_helpers import provider_for

issuer = _fixtures.issuer
provider = _fixtures.provider


@pytest.mark.req("AUTH-09")
def test_exchange_code_posts_to_the_token_endpoint_with_client_credentials(
    issuer: FakeIssuer, provider: OidcProvider
) -> None:
    body = provider.token_request({"grant_type": "authorization_code", "code": "abc"})
    assert body["token_type"] == "Bearer"
    sent = issuer.requests_to("/token")[-1]
    assert sent.form["code"] == "abc"
    assert sent.form["client_id"] == "hp"
    assert sent.form["client_secret"] == issuer.client_secret


@pytest.mark.req("AUTH-09")
def test_token_request_errors_become_auth_errors(
    issuer: FakeIssuer, provider: OidcProvider
) -> None:
    issuer.down()
    with pytest.raises(AuthError) as info:
        provider.token_request({"grant_type": "authorization_code", "code": "abc"})
    assert info.value.status_code == 502
    issuer.up()
    issuer.token_response = {"error": "invalid_grant"}
    broken = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(400, json={"error": "invalid_grant"})
        )
    )
    provider._http = broken
    with pytest.raises(AuthError) as info2:
        provider.token_request({"grant_type": "authorization_code", "code": "bad"})
    assert info2.value.status_code == 401


@pytest.mark.req("AUTH-10")
def test_introspect_active_token_passes_with_client_credentials(
    issuer: FakeIssuer, provider: OidcProvider
) -> None:
    token = issuer.token(roles=["hp-deleter"])
    provider.introspect(token)
    sent = issuer.requests_to("/introspect")[-1]
    assert sent.form == {"token": token, "token_type_hint": "access_token"}
    assert sent.headers["authorization"].startswith("Basic ")
    assert sent.method == "POST"


@pytest.mark.req("AUTH-10")
def test_introspect_inactive_token_is_401(issuer: FakeIssuer, provider: OidcProvider) -> None:
    issuer.set_active(False)
    with pytest.raises(AuthError) as info:
        provider.introspect(issuer.token())
    assert info.value.status_code == 401
    assert info.value.detail == "token is not active"


@pytest.mark.req("AUTH-10")
def test_introspect_is_never_cached(issuer: FakeIssuer, provider: OidcProvider) -> None:
    token = issuer.token()
    provider.introspect(token)
    provider.introspect(token)
    assert len(issuer.requests_to("/introspect")) == 2
    issuer.set_active(False)
    with pytest.raises(AuthError):
        provider.introspect(token)  # revocation takes effect at once


@pytest.mark.req("AUTH-10")
def test_introspect_unreachable_issuer_is_502(issuer: FakeIssuer, provider: OidcProvider) -> None:
    issuer.down()
    with pytest.raises(AuthError) as info:
        provider.introspect(issuer.token())
    assert info.value.status_code == 502
    assert info.value.detail == "identity provider unavailable"


@pytest.mark.req("AUTH-10")
@pytest.mark.parametrize("status", [500, 502, 503, 401, 403])
def test_introspect_error_status_is_502(
    issuer: FakeIssuer, provider: OidcProvider, status: int
) -> None:
    issuer.introspect_status = status
    with pytest.raises(AuthError) as info:
        provider.introspect(issuer.token())
    assert info.value.status_code == 502


@pytest.mark.req("AUTH-10")
def test_introspect_malformed_answer_is_502(tmp_path: Path, issuer: FakeIssuer) -> None:
    provider = provider_for(tmp_path, issuer)
    provider._http = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text="oops"))
    )
    with pytest.raises(AuthError) as info:
        provider.introspect("tok")
    assert info.value.status_code == 502


@pytest.mark.req("AUTH-10")
@pytest.mark.parametrize("body", [{}, {"active": "true"}, {"active": None}, ["active"]])
def test_introspect_only_a_literal_true_is_active(
    tmp_path: Path, issuer: FakeIssuer, body: Any
) -> None:
    provider = provider_for(tmp_path, issuer)
    provider._http = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))
    )
    with pytest.raises(AuthError) as info:
        provider.introspect("tok")
    assert info.value.status_code == 401


@pytest.mark.req("AUTH-10")
def test_secret_and_token_never_appear_in_errors_or_logs(
    issuer: FakeIssuer, provider: OidcProvider, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    token = issuer.token()
    issuer.down()
    with pytest.raises(AuthError) as info:
        provider.introspect(token)
    assert DEFAULT_CLIENT_SECRET not in info.value.detail
    assert token not in info.value.detail
    issuer.up()
    issuer.set_active(False)
    with pytest.raises(AuthError):
        provider.introspect(token)
    assert DEFAULT_CLIENT_SECRET not in caplog.text
    assert token not in caplog.text

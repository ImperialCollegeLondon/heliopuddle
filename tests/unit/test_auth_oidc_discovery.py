"""OIDC discovery against a fake issuer (AUTH-09)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from heliopuddle.server.auth.errors import AuthError
from heliopuddle.server.auth.oidc import OidcProvider, OidcStartupError
from tests.fake_oidc import DEFAULT_CLIENT_SECRET, FakeIssuer
from tests.unit import _auth_oidc_helpers as _fixtures
from tests.unit._auth_oidc_helpers import make_settings

issuer = _fixtures.issuer
provider = _fixtures.provider


@pytest.mark.req("AUTH-09")
def test_discover_exposes_endpoints_and_fetches_jwks_once(
    tmp_path: Path, issuer: FakeIssuer, provider: OidcProvider
) -> None:
    assert provider.issuer == issuer.issuer
    assert provider.client_id == "hp"
    assert provider.token_url == issuer.discovery["token_endpoint"]
    assert provider.token_endpoint == provider.token_url
    assert provider.authorization_url == issuer.discovery["authorization_endpoint"]
    assert provider.introspection_url == issuer.discovery["introspection_endpoint"]
    assert issuer.jwks_calls == 1
    provider.authenticate(issuer.token(roles=["hp-reader"]))
    provider.authenticate(issuer.token(roles=["hp-reader"]))
    assert issuer.jwks_calls == 1  # cached


@pytest.mark.req("AUTH-09")
def test_discover_unreachable_names_the_issuer_setting(tmp_path: Path, issuer: FakeIssuer) -> None:
    issuer.down()
    with pytest.raises(OidcStartupError) as info:
        OidcProvider.discover(make_settings(tmp_path, issuer), issuer.client())
    message = str(info.value)
    assert "HELIOPUDDLE_OIDC_ISSUER" in message
    assert "connection refused" in message
    assert DEFAULT_CLIENT_SECRET not in message


@pytest.mark.req("AUTH-09")
def test_discover_http_error_status(tmp_path: Path) -> None:
    broken = FakeIssuer()
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    with pytest.raises(OidcStartupError, match=r"HELIOPUDDLE_OIDC_ISSUER.*503"):
        OidcProvider.discover(make_settings(tmp_path, broken), client)


@pytest.mark.req("AUTH-09")
def test_discover_rejects_a_document_that_is_not_json(tmp_path: Path) -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<hi>")))
    with pytest.raises(OidcStartupError, match=r"HELIOPUDDLE_OIDC_ISSUER.*JSON"):
        OidcProvider.discover(make_settings(tmp_path, FakeIssuer()), client)


@pytest.mark.req("AUTH-09")
def test_discover_rejects_issuer_mismatch(tmp_path: Path, issuer: FakeIssuer) -> None:
    issuer.discovery["issuer"] = "https://other.test/realms/x"
    with pytest.raises(OidcStartupError, match=r"HELIOPUDDLE_OIDC_ISSUER.*other.test"):
        OidcProvider.discover(make_settings(tmp_path, issuer), issuer.client())


@pytest.mark.req("AUTH-09")
def test_discover_requires_jwks_uri(tmp_path: Path, issuer: FakeIssuer) -> None:
    del issuer.discovery["jwks_uri"]
    with pytest.raises(OidcStartupError, match=r"HELIOPUDDLE_OIDC_ISSUER.*jwks_uri"):
        OidcProvider.discover(make_settings(tmp_path, issuer), issuer.client())


@pytest.mark.req("AUTH-09")
def test_discover_fails_when_the_jwks_is_unreachable(tmp_path: Path, issuer: FakeIssuer) -> None:
    issuer.down(jwks_only=True)
    with pytest.raises(OidcStartupError, match=r"HELIOPUDDLE_OIDC_ISSUER.*JWKS"):
        OidcProvider.discover(make_settings(tmp_path, issuer), issuer.client())


@pytest.mark.req("AUTH-09")
def test_discover_fails_when_the_jwks_has_no_usable_key(tmp_path: Path, issuer: FakeIssuer) -> None:
    issuer._keys.clear()
    issuer.jwks_extra_keys = [{"kty": "oct", "kid": "sym", "k": "c2VjcmV0"}]
    with pytest.raises(OidcStartupError, match=r"HELIOPUDDLE_OIDC_ISSUER.*usable"):
        OidcProvider.discover(make_settings(tmp_path, issuer), issuer.client())


@pytest.mark.req("AUTH-10")
def test_discover_requires_introspection_endpoint_when_enabled(
    tmp_path: Path, issuer: FakeIssuer
) -> None:
    del issuer.discovery["introspection_endpoint"]
    with pytest.raises(
        OidcStartupError, match=r"HELIOPUDDLE_OIDC_INTROSPECT.*introspection_endpoint"
    ):
        OidcProvider.discover(make_settings(tmp_path, issuer), issuer.client())


@pytest.mark.req("AUTH-10")
def test_introspection_endpoint_is_optional_when_off(tmp_path: Path, issuer: FakeIssuer) -> None:
    del issuer.discovery["introspection_endpoint"]
    del issuer.discovery["authorization_endpoint"]
    provider = OidcProvider.discover(
        make_settings(tmp_path, issuer, oidc_introspect="off", oidc_client_secret=None),
        issuer.client(),
    )
    assert provider.introspection_url is None
    assert provider.authorization_url is None
    with pytest.raises(AuthError) as info:
        provider.introspect("tok")
    assert info.value.status_code == 502


@pytest.mark.req("AUTH-09")
def test_discover_without_a_client_builds_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, issuer: FakeIssuer
) -> None:
    real = httpx.Client
    monkeypatch.setattr(
        "heliopuddle.server.auth.oidc.httpx.Client",
        lambda **kw: real(transport=httpx.MockTransport(issuer._handle), **kw),
    )
    provider = OidcProvider.discover(make_settings(tmp_path, issuer))
    assert provider.authenticate(issuer.token(roles=["hp-admin"])).roles == ("hp-admin",)
    provider.close()

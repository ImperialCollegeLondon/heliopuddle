"""OIDC discovery from a separate address (AUTH-21).

hp may reach the provider at another address than browsers do (a provider on a
private network, whose issuer is its public URL). ``HELIOPUDDLE_OIDC_DISCOVERY_URL``
says where to read the discovery document; the endpoints in it are used as they
are, and the issuer is still checked against ``HELIOPUDDLE_OIDC_ISSUER``.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from heliopuddle.server.auth.oidc import OidcProvider, OidcStartupError
from tests.fake_oidc import FakeIssuer
from tests.unit._auth_oidc_helpers import make_settings

PUBLIC = "https://idp.example.test/realms/hp"
INTERNAL = "http://idp-internal:8080/realms/hp"
DISCOVERY = f"{INTERNAL}/.well-known/openid-configuration"


@pytest.fixture
def split_issuer() -> FakeIssuer:
    """An issuer whose server-to-server endpoints are on the internal address."""
    return FakeIssuer(
        issuer=PUBLIC,
        discovery={
            "jwks_uri": f"{INTERNAL}/protocol/openid-connect/certs",
            "token_endpoint": f"{INTERNAL}/protocol/openid-connect/token",
            "introspection_endpoint": f"{INTERNAL}/protocol/openid-connect/token/introspect",
        },
    )


def _recording(issuer: FakeIssuer, urls: list[str]) -> httpx.Client:
    def handle(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return issuer._handle(request)

    return httpx.Client(transport=httpx.MockTransport(handle))


@pytest.mark.req("AUTH-21")
def test_discovery_is_read_from_the_discovery_url(tmp_path: Path, split_issuer: FakeIssuer) -> None:
    urls: list[str] = []
    settings = make_settings(tmp_path, split_issuer, oidc_discovery_url=DISCOVERY)
    provider = OidcProvider.discover(settings, _recording(split_issuer, urls))
    assert urls == [DISCOVERY]
    # the issuer stays the public one, the endpoints are the document's own
    assert provider.issuer == PUBLIC
    assert provider.token_url == f"{INTERNAL}/protocol/openid-connect/token"
    assert provider.authorization_url == f"{PUBLIC}/protocol/openid-connect/auth"
    assert provider.end_session_url == f"{PUBLIC}/protocol/openid-connect/logout"
    # tokens issued by the public issuer validate against the internally fetched JWKS
    principal = provider.authenticate(split_issuer.token(roles=["hp-reader"]))
    assert principal.roles == ("hp-reader",)
    assert split_issuer.jwks_calls == 1


@pytest.mark.req("AUTH-21")
def test_introspection_uses_the_documents_endpoint(
    tmp_path: Path, split_issuer: FakeIssuer
) -> None:
    urls: list[str] = []
    settings = make_settings(tmp_path, split_issuer, oidc_discovery_url=DISCOVERY)
    provider = OidcProvider.discover(settings, _recording(split_issuer, urls))
    provider.introspect(split_issuer.token(roles=["hp-admin"]))
    assert urls[-1] == f"{INTERNAL}/protocol/openid-connect/token/introspect"


@pytest.mark.req("AUTH-21")
def test_the_issuer_is_still_checked(tmp_path: Path, split_issuer: FakeIssuer) -> None:
    split_issuer.discovery["issuer"] = INTERNAL
    settings = make_settings(tmp_path, split_issuer, oidc_discovery_url=DISCOVERY)
    with pytest.raises(OidcStartupError, match=r"HELIOPUDDLE_OIDC_ISSUER.*idp-internal"):
        OidcProvider.discover(settings, split_issuer.client())


@pytest.mark.req("AUTH-21")
def test_an_unreachable_discovery_url_names_its_setting(
    tmp_path: Path, split_issuer: FakeIssuer
) -> None:
    split_issuer.down()
    settings = make_settings(tmp_path, split_issuer, oidc_discovery_url=DISCOVERY)
    with pytest.raises(OidcStartupError) as info:
        OidcProvider.discover(settings, split_issuer.client())
    assert "HELIOPUDDLE_OIDC_DISCOVERY_URL" in str(info.value)
    assert DISCOVERY in str(info.value)


@pytest.mark.req("AUTH-21")
def test_without_the_setting_discovery_is_under_the_issuer(
    tmp_path: Path, issuer: FakeIssuer
) -> None:
    urls: list[str] = []
    OidcProvider.discover(make_settings(tmp_path, issuer), _recording(issuer, urls))
    assert urls == [f"{issuer.issuer}/.well-known/openid-configuration"]


@pytest.fixture
def issuer() -> FakeIssuer:
    return FakeIssuer()

"""OIDC token claims, roles and audience (AUTH-09, AUTH-10)."""

from __future__ import annotations

from pathlib import Path

import pytest

from heliopuddle.server.auth.oidc import OidcProvider
from tests.fake_oidc import FakeIssuer
from tests.unit import _auth_oidc_helpers as _fixtures
from tests.unit._auth_oidc_helpers import provider_for, reject

issuer = _fixtures.issuer
provider = _fixtures.provider


@pytest.mark.req("AUTH-09")
def test_valid_token_yields_a_principal(issuer: FakeIssuer, provider: OidcProvider) -> None:
    token = issuer.token(username="ann", roles=["hp-reader", "hp-writer:demo"])
    principal = provider.authenticate(token)
    assert principal.username == "ann"
    assert principal.roles == ("hp-reader", "hp-writer:demo")
    assert principal.mode == "oidc"
    assert principal.token == token


@pytest.mark.req("AUTH-09")
def test_username_falls_back_to_sub(issuer: FakeIssuer, provider: OidcProvider) -> None:
    principal = provider.authenticate(issuer.token(sub="abc-123", username=None, roles=[]))
    assert principal.username == "abc-123"


@pytest.mark.req("AUTH-09")
def test_non_hp_roles_are_dropped_and_scoped_roles_kept(
    issuer: FakeIssuer, provider: OidcProvider
) -> None:
    roles = ["offline_access", "uma_authorization", "hp-deleter:demo", "hp-bogus", "hp-admin"]
    principal = provider.authenticate(issuer.token(roles=roles))
    assert principal.roles == ("hp-deleter:demo", "hp-admin")


@pytest.mark.req("AUTH-09")
def test_missing_roles_claim_authenticates_with_no_roles(
    issuer: FakeIssuer, provider: OidcProvider
) -> None:
    principal = provider.authenticate(issuer.token(roles=None))
    assert principal.username == "ann"
    assert principal.roles == ()


@pytest.mark.req("AUTH-09")
def test_a_token_with_only_foreign_roles_still_authenticates(
    issuer: FakeIssuer, provider: OidcProvider
) -> None:
    assert provider.authenticate(issuer.token(roles=["offline_access"])).roles == ()


@pytest.mark.req("AUTH-09")
def test_roles_claim_of_the_wrong_shape_gives_no_roles(
    issuer: FakeIssuer, provider: OidcProvider
) -> None:
    token = issuer.token(roles=None, extra_claims={"realm_access": {"roles": "hp-admin"}})
    assert provider.authenticate(token).roles == ()
    token = issuer.token(roles=None, extra_claims={"realm_access": ["hp-admin"]})
    assert provider.authenticate(token).roles == ()
    token = issuer.token(roles=None, extra_claims={"realm_access": {"roles": [1, "hp-reader"]}})
    assert provider.authenticate(token).roles == ("hp-reader",)


@pytest.mark.req("AUTH-09")
def test_custom_roles_and_username_claims(tmp_path: Path, issuer: FakeIssuer) -> None:
    provider = provider_for(
        tmp_path, issuer, oidc_roles_claim="resource_access.hp.roles", oidc_username_claim="email"
    )
    token = issuer.token(
        roles=["hp-admin"],  # default location is ignored now
        extra_claims={
            "resource_access": {"hp": {"roles": ["hp-writer"]}},
            "email": "ann@example.org",
        },
    )
    principal = provider.authenticate(token)
    assert principal.username == "ann@example.org"
    assert principal.roles == ("hp-writer",)


@pytest.mark.req("AUTH-09")
def test_roles_claim_may_be_a_flat_key_containing_dots(tmp_path: Path, issuer: FakeIssuer) -> None:
    provider = provider_for(tmp_path, issuer, oidc_roles_claim="https://example.org/roles")
    token = issuer.token(roles=None, extra_claims={"https://example.org/roles": ["hp-reader"]})
    assert provider.authenticate(token).roles == ("hp-reader",)


@pytest.mark.req("AUTH-09")
def test_audience_may_be_a_list(issuer: FakeIssuer, provider: OidcProvider) -> None:
    assert provider.authenticate(issuer.token(aud=["account", "hp"], roles=["hp-reader"]))


@pytest.mark.req("AUTH-09")
def test_audience_defaults_to_the_client_id_and_can_be_set(
    tmp_path: Path, issuer: FakeIssuer
) -> None:
    provider = provider_for(tmp_path, issuer, oidc_audience="account")
    assert provider.authenticate(issuer.token(aud="account", roles=[]))
    reject(provider, issuer.token(aud="hp", roles=[]))

"""OIDC token rejection and key rotation (AUTH-09)."""

from __future__ import annotations

from pathlib import Path

import pytest
from jwt import PyJWKClient

from heliopuddle.server.auth.oidc import OidcProvider
from tests.fake_oidc import FakeIssuer
from tests.unit import _auth_oidc_helpers as _fixtures
from tests.unit._auth_oidc_helpers import provider_for, reject

issuer = _fixtures.issuer
provider = _fixtures.provider


@pytest.mark.req("AUTH-09")
def test_rejects_expired_token(issuer: FakeIssuer, provider: OidcProvider) -> None:
    error = reject(provider, issuer.token(exp_delta=-3600))
    assert error.detail == "invalid token"


@pytest.mark.req("AUTH-09")
def test_rejects_token_without_exp(issuer: FakeIssuer, provider: OidcProvider) -> None:
    reject(provider, issuer.token(exp_delta=None))


@pytest.mark.req("AUTH-09")
def test_rejects_wrong_issuer(issuer: FakeIssuer, provider: OidcProvider) -> None:
    reject(provider, issuer.token(iss="https://evil.test/realms/hp"))


@pytest.mark.req("AUTH-09")
def test_rejects_missing_issuer(issuer: FakeIssuer, provider: OidcProvider) -> None:
    reject(provider, issuer.token(extra_claims={"iss": None}))


@pytest.mark.req("AUTH-09")
def test_rejects_wrong_audience_string(issuer: FakeIssuer, provider: OidcProvider) -> None:
    reject(provider, issuer.token(aud="someone-else"))


@pytest.mark.req("AUTH-09")
def test_rejects_wrong_audience_list(issuer: FakeIssuer, provider: OidcProvider) -> None:
    reject(provider, issuer.token(aud=["account", "other"]))


@pytest.mark.req("AUTH-09")
def test_rejects_missing_audience(issuer: FakeIssuer, provider: OidcProvider) -> None:
    reject(provider, issuer.token(aud=None))


@pytest.mark.req("AUTH-09")
def test_rejects_algorithm_confusion_hs256(issuer: FakeIssuer, provider: OidcProvider) -> None:
    # signed with the issuer's public key as the HMAC secret
    for alg in ("HS256", "HS384", "HS512"):
        reject(provider, issuer.token(alg=alg, roles=["hp-admin"]))


@pytest.mark.req("AUTH-09")
def test_rejects_hs256_even_when_the_jwks_offers_a_symmetric_key(
    tmp_path: Path, issuer: FakeIssuer
) -> None:
    issuer.jwks_extra_keys = [{"kty": "oct", "kid": "sym", "alg": "HS256", "k": "c2VjcmV0"}]
    provider = provider_for(tmp_path, issuer)
    reject(provider, issuer.token(alg="HS256", kid="sym", roles=["hp-admin"]))


@pytest.mark.req("AUTH-09")
def test_rejects_alg_none(issuer: FakeIssuer, provider: OidcProvider) -> None:
    reject(provider, issuer.token(alg="none", roles=["hp-admin"]))


@pytest.mark.req("AUTH-09")
def test_rejects_tampered_signature_and_payload(issuer: FakeIssuer, provider: OidcProvider) -> None:
    token = issuer.token(roles=["hp-reader"])
    head, payload, sig = token.split(".")
    flipped = ("A" if sig[0] != "A" else "B") + sig[1:]
    reject(provider, f"{head}.{payload}.{flipped}")
    other = issuer.token(roles=["hp-admin"]).split(".")[1]
    reject(provider, f"{head}.{other}.{sig}")


@pytest.mark.req("AUTH-09")
@pytest.mark.parametrize("token", ["", "abc", "a.b.c", "....", "not a token"])
def test_rejects_garbage(provider: OidcProvider, token: str) -> None:
    reject(provider, token)


@pytest.mark.req("AUTH-09")
def test_rejects_token_without_kid(issuer: FakeIssuer, provider: OidcProvider) -> None:
    import jwt as pyjwt

    token = pyjwt.encode(
        {"iss": issuer.issuer, "aud": "hp", "exp": 9999999999},
        issuer.private_key,
        algorithm="RS256",
    )
    reject(provider, token)


@pytest.mark.req("AUTH-09")
def test_rejects_a_token_signed_by_an_unpublished_key(
    issuer: FakeIssuer, provider: OidcProvider
) -> None:
    other = FakeIssuer()
    reject(provider, other.token(kid=issuer.kid))  # right kid, wrong key
    reject(provider, other.token(kid="never-seen"))


@pytest.mark.req("AUTH-09")
def test_rotated_key_is_accepted_after_a_refetch(
    issuer: FakeIssuer, provider: OidcProvider
) -> None:
    old = issuer.token(roles=["hp-reader"])
    assert provider.authenticate(old)
    issuer.rotate_key(keep_old=True)
    calls = issuer.jwks_calls
    assert provider.authenticate(issuer.token(roles=["hp-reader"]))  # unknown kid: refetch
    assert issuer.jwks_calls == calls + 1
    assert provider.authenticate(old)  # old key still published


@pytest.mark.req("AUTH-09")
def test_retired_key_stops_working_after_rotation(
    issuer: FakeIssuer, provider: OidcProvider
) -> None:
    old = issuer.token(roles=["hp-reader"])
    issuer.rotate_key()
    assert provider.authenticate(issuer.token(roles=["hp-reader"]))
    reject(provider, old)  # the old kid is no longer published; cache was replaced


@pytest.mark.req("AUTH-09")
def test_unknown_kid_refetches_are_throttled(tmp_path: Path, issuer: FakeIssuer) -> None:
    provider = provider_for(tmp_path, issuer, cooldown=3600)
    calls = issuer.jwks_calls
    for _ in range(5):
        reject(provider, issuer.token(kid="nope"))
    assert issuer.jwks_calls == calls  # a flood of bad kids must not hammer the issuer


@pytest.mark.req("AUTH-09")
def test_keys_come_from_a_pyjwkclient_that_caches_the_key_set(
    issuer: FakeIssuer, provider: OidcProvider
) -> None:
    assert isinstance(provider._jwks, PyJWKClient)
    assert provider._jwks.uri == issuer.discovery["jwks_uri"]
    calls = issuer.jwks_calls
    for _ in range(3):
        assert provider.authenticate(issuer.token(roles=["hp-reader"]))
    assert issuer.jwks_calls == calls  # the key set is cached, not refetched per request


@pytest.mark.req("AUTH-09")
def test_unreachable_jwks_on_refetch_is_a_502(issuer: FakeIssuer, provider: OidcProvider) -> None:
    issuer.rotate_key()
    issuer.down(jwks_only=True)
    error = reject(provider, issuer.token(roles=[]), status=502)
    assert error.detail == "identity provider unavailable"
    issuer.up()
    assert provider.authenticate(issuer.token(roles=[]))


@pytest.mark.req("AUTH-09")
def test_failure_messages_do_not_leak_details(issuer: FakeIssuer, provider: OidcProvider) -> None:
    for token in (issuer.token(exp_delta=-3600), issuer.token(aud="x"), "garbage"):
        assert reject(provider, token).detail == "invalid token"

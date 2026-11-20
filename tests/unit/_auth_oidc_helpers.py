"""Shared settings and fake issuer fixtures for the OIDC tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from heliopuddle.server.auth.errors import AuthError
from heliopuddle.server.auth.oidc import OidcProvider
from heliopuddle.settings import Settings
from tests.fake_oidc import FakeIssuer


def make_settings(tmp_path: Path, issuer: FakeIssuer, **kwargs: Any) -> Settings:
    values: dict[str, Any] = {
        "auth_mode": "oidc",
        "oidc_issuer": issuer.issuer + "/",  # a trailing slash is tolerated
        "oidc_client_id": issuer.client_id,
        "oidc_client_secret": issuer.client_secret,
        "oidc_introspect": "sensitive",
    }
    values.update(kwargs)
    return Settings(config_dir=tmp_path / "c", data_dir=tmp_path / "d", **values)


@pytest.fixture
def issuer() -> FakeIssuer:
    return FakeIssuer()


def provider_for(
    tmp_path: Path, issuer: FakeIssuer, *, cooldown: float = 0, **kwargs: Any
) -> OidcProvider:
    """A provider; ``cooldown=0`` lets tests rotate keys and see the refetch at once."""
    return OidcProvider.discover(
        make_settings(tmp_path, issuer, **kwargs), issuer.client(), jwks_cooldown=cooldown
    )


@pytest.fixture
def provider(tmp_path: Path, issuer: FakeIssuer) -> OidcProvider:
    return provider_for(tmp_path, issuer)


def reject(provider: OidcProvider, token: str, status: int = 401) -> AuthError:
    with pytest.raises(AuthError) as info:
        provider.authenticate(token)
    assert info.value.status_code == status
    return info.value

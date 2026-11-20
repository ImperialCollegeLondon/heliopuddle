"""Auth settings and mode validation (AUTH-01, AUTH-03, AUTH-06, AUTH-09, AUTH-10)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from heliopuddle.server.start import StartupError, load_settings
from heliopuddle.settings import Settings


def make(tmp_path: Path, **kwargs: object) -> Settings:
    return Settings(config_dir=tmp_path / "c", data_dir=tmp_path / "d", **kwargs)  # type: ignore[arg-type]


@pytest.mark.req("AUTH-01")
def test_default_mode_is_local_and_secure_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HELIOPUDDLE_AUTH_MODE")
    s = make(tmp_path)
    assert s.auth_mode == "local"
    assert s.admin_username == "admin"
    assert s.admin_password is None
    assert s.admin_reset is False
    assert s.auth_token_ttl == timedelta(hours=8)
    assert s.oidc_introspect == "sensitive"
    assert s.oidc_username_claim == "preferred_username"
    assert s.oidc_roles_claim == "realm_access.roles"


@pytest.mark.req("AUTH-01")
def test_mode_comes_from_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HELIOPUDDLE_AUTH_MODE", "header")
    assert make(tmp_path).auth_mode == "header"


@pytest.mark.req("AUTH-01")
def test_bad_mode_stops_startup_naming_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HELIOPUDDLE_AUTH_MODE", "ldap")
    with pytest.raises(StartupError, match="HELIOPUDDLE_AUTH_MODE"):
        load_settings()


@pytest.mark.req("AUTH-09")
def test_oidc_needs_issuer_and_client_id(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="HELIOPUDDLE_OIDC_ISSUER"):
        make(tmp_path, auth_mode="oidc", oidc_client_id="hp", oidc_client_secret="s")
    with pytest.raises(ValidationError, match="HELIOPUDDLE_OIDC_CLIENT_ID"):
        make(tmp_path, auth_mode="oidc", oidc_issuer="https://kc/realms/r", oidc_client_secret="s")


@pytest.mark.req("AUTH-09")
def test_oidc_audience_defaults_to_the_client_id(tmp_path: Path) -> None:
    s = make(
        tmp_path,
        auth_mode="oidc",
        oidc_issuer="https://kc/realms/r/",
        oidc_client_id="hp",
        oidc_client_secret="s",
    )
    assert s.resolved_oidc_audience == "hp"
    assert s.resolved_oidc_issuer == "https://kc/realms/r"
    s = s.model_copy(update={"oidc_audience": "account"})
    assert s.resolved_oidc_audience == "account"


@pytest.mark.req("AUTH-10")
def test_introspection_needs_a_client_secret_unless_off(tmp_path: Path) -> None:
    base = {
        "auth_mode": "oidc",
        "oidc_issuer": "https://kc/realms/r",
        "oidc_client_id": "hp",
    }
    with pytest.raises(ValidationError, match="HELIOPUDDLE_OIDC_CLIENT_SECRET"):
        make(tmp_path, **base)
    with pytest.raises(ValidationError, match="HELIOPUDDLE_OIDC_CLIENT_SECRET"):
        make(tmp_path, oidc_introspect="always", **base)
    assert make(tmp_path, oidc_introspect="off", **base).oidc_introspect == "off"


@pytest.mark.req("AUTH-10")
def test_oidc_introspect_value_is_validated(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="oidc_introspect"):
        make(tmp_path, oidc_introspect="sometimes")


@pytest.mark.req("AUTH-09")
def test_oidc_settings_are_not_needed_in_other_modes(tmp_path: Path) -> None:
    assert make(tmp_path, auth_mode="local").oidc_issuer is None
    assert make(tmp_path, auth_mode="header").oidc_issuer is None


@pytest.mark.req("AUTH-03")
def test_admin_password_minimum_length(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="at least 12"):
        make(tmp_path, admin_password="short")
    s = make(tmp_path, admin_password="a-long-enough-password")
    assert s.admin_password is not None
    assert s.admin_password.get_secret_value() == "a-long-enough-password"
    assert "a-long-enough-password" not in repr(s)


@pytest.mark.req("AUTH-03")
def test_admin_reset_needs_the_password(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="HELIOPUDDLE_ADMIN_PASSWORD"):
        make(tmp_path, admin_reset=True)
    assert make(tmp_path, admin_reset=True, admin_password="a-long-enough-password").admin_reset


@pytest.mark.req("AUTH-05")
def test_token_ttl_is_a_positive_duration(tmp_path: Path) -> None:
    assert make(tmp_path, auth_token_ttl="30m").auth_token_ttl == timedelta(minutes=30)
    with pytest.raises(ValidationError, match="greater than zero"):
        make(tmp_path, auth_token_ttl="0s")


@pytest.mark.req("AUTH-06")
def test_secret_key_is_secret(tmp_path: Path) -> None:
    s = make(tmp_path, auth_secret_key="x" * 40)
    assert s.auth_secret_key is not None
    assert "x" * 40 not in repr(s)


@pytest.mark.req("AUTH-03")
@pytest.mark.parametrize("name", ["a/b", "a b", "Älice", ".x", "", "x" * 256])
def test_admin_username_uses_the_same_alphabet_as_users(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValidationError, match="admin_username"):
        make(tmp_path, admin_username=name)


@pytest.mark.req("AUTH-03")
def test_admin_username_accepts_the_user_alphabet(tmp_path: Path) -> None:
    assert make(tmp_path, admin_username="Root.Admin+1@example.org").admin_username

"""Auth initialisation during ``server start`` (AUTH-01, AUTH-03, AUTH-06, AUTH-09)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from sqlalchemy import func, select

from heliopuddle.server.auth import oidc, secret
from heliopuddle.server.context import set_context
from heliopuddle.server.database.orm import User
from heliopuddle.server.start import EXIT_CONFIG, StartupError, prepare_database
from heliopuddle.settings import Settings
from tests.fake_oidc import FakeIssuer


def settings_for(tmp_path: Path, config_dir: Path, **kwargs: object) -> Settings:
    return Settings(config_dir=config_dir, data_dir=tmp_path / "data", **kwargs)  # type: ignore[arg-type]


def user_count(settings: Settings) -> int:
    ctx = prepare_database(settings)
    try:
        with ctx.sessionmaker() as session:
            return session.scalar(select(func.count()).select_from(User)) or 0
    finally:
        set_context(None)
        ctx.dispose()


@pytest.mark.req("AUTH-03")
def test_local_mode_start_creates_the_key_and_one_admin(
    tmp_path: Path, config_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = settings_for(tmp_path, config_dir, auth_mode="local")
    with caplog.at_level(logging.WARNING):
        assert user_count(settings) == 1
    assert (tmp_path / "data" / "auth_secret").exists()
    assert sum("admin" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)
    # A second start neither adds a user nor logs another password.
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        assert user_count(settings) == 1
    assert not [r for r in caplog.records if "password" in r.getMessage().lower()]


@pytest.mark.req("AUTH-01")
def test_header_mode_start_creates_no_users_and_no_key(tmp_path: Path, config_dir: Path) -> None:
    settings = settings_for(tmp_path, config_dir, auth_mode="header")
    assert user_count(settings) == 0
    assert not (tmp_path / "data" / "auth_secret").exists()


@pytest.mark.req("AUTH-06")
def test_a_missing_signing_key_stops_startup_with_the_config_exit_code(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_key(_settings: Settings) -> bytes:
        raise secret.SecretKeyError("set HELIOPUDDLE_AUTH_SECRET_KEY")

    monkeypatch.setattr(secret, "load_secret", no_key)
    with pytest.raises(StartupError, match="HELIOPUDDLE_AUTH_SECRET_KEY") as error:
        prepare_database(settings_for(tmp_path, config_dir, auth_mode="local"))
    assert error.value.exit_code == EXIT_CONFIG


@pytest.mark.req("AUTH-09")
def test_oidc_mode_start_discovers_the_provider(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    issuer = FakeIssuer()
    real = oidc.OidcProvider.discover.__func__  # type: ignore[attr-defined]
    monkeypatch.setattr(
        oidc.OidcProvider,
        "discover",
        classmethod(lambda cls, settings, http_client=None: real(cls, settings, issuer.client())),
    )
    settings = settings_for(
        tmp_path,
        config_dir,
        auth_mode="oidc",
        oidc_issuer=issuer.issuer,
        oidc_client_id=issuer.client_id,
        oidc_client_secret=issuer.client_secret,
    )
    ctx = prepare_database(settings)
    try:
        assert isinstance(ctx.oidc, oidc.OidcProvider)
        assert ctx.oidc.issuer == issuer.issuer
        with ctx.sessionmaker() as session:
            assert session.scalar(select(func.count()).select_from(User)) == 0
    finally:
        set_context(None)
        ctx.dispose()


@pytest.mark.req("AUTH-09")
def test_oidc_mode_start_fails_fast_when_the_issuer_is_unreachable(
    tmp_path: Path, config_dir: Path
) -> None:
    settings = settings_for(
        tmp_path,
        config_dir,
        auth_mode="oidc",
        oidc_issuer="http://127.0.0.1:9/realms/none",
        oidc_client_id="hp",
        oidc_client_secret="s3cret-value",
    )
    with pytest.raises(StartupError, match="HELIOPUDDLE_OIDC_ISSUER") as error:
        prepare_database(settings)
    assert error.value.exit_code == EXIT_CONFIG
    assert "s3cret-value" not in str(error.value)


@pytest.mark.req("AUTH-20")
@pytest.mark.parametrize(
    ("mode", "host", "warns"),
    [
        ("header", "0.0.0.0", True),
        ("header", "192.168.1.5", True),
        ("header", "127.0.0.1", False),
        ("header", "::1", False),
        ("header", "localhost", False),
        ("local", "0.0.0.0", False),
    ],
)
def test_header_mode_warns_when_not_bound_to_loopback(
    tmp_path: Path, mode: str, host: str, warns: bool, caplog: pytest.LogCaptureFixture
) -> None:
    from heliopuddle.server.start import warn_if_header_mode_exposed

    settings = Settings(data_dir=tmp_path, auth_mode=mode, host=host)  # type: ignore[arg-type]
    with caplog.at_level(logging.WARNING):
        warn_if_header_mode_exposed(settings)
    warned = [r for r in caplog.records if r.levelno == logging.WARNING and "header" in r.message]
    assert bool(warned) is warns

"""The UI session secret at startup (AUTH-16, AUTH-06)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from heliopuddle.server.auth.secret import SecretKeyError
from heliopuddle.server.start import StartOptions, StartupError, plan_servers
from heliopuddle.server.ui import create_ui
from heliopuddle.settings import Settings
from tests.unit._ui_auth_helpers import SECRET, AuthFactory, make_settings

pytestmark = pytest.mark.req("AUTH-16")


def blocked_data_dir(tmp_path: Path) -> Path:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x")
    return blocker / "data"


@pytest.mark.req("AUTH-06")
@pytest.mark.parametrize("mode", ["local", "oidc"])
def test_create_ui_fails_fast_without_a_secret(tmp_path: Path, mode: str) -> None:
    extra: dict[str, Any] = {}
    if mode == "oidc":
        extra = {
            "oidc_issuer": "https://idp.test/realms/hp",
            "oidc_client_id": "hp",
            "oidc_client_secret": "s3cret-value",
        }
    settings = make_settings(auth_mode=mode, data_dir=blocked_data_dir(tmp_path), **extra)
    with pytest.raises(SecretKeyError, match="HELIOPUDDLE_AUTH_SECRET_KEY"):
        create_ui(settings, client_factory=AuthFactory())


@pytest.mark.req("AUTH-06")
def test_create_ui_needs_no_secret_in_header_mode_or_with_the_env_var(tmp_path: Path) -> None:
    bad = blocked_data_dir(tmp_path)
    create_ui(Settings(auth_mode="header", data_dir=bad), client_factory=AuthFactory())
    create_ui(
        make_settings(auth_mode="local", data_dir=bad, auth_secret_key=SECRET),
        client_factory=AuthFactory(),
    )


@pytest.mark.req("AUTH-06")
def test_create_ui_generates_the_secret_file_in_a_writable_data_dir(tmp_path: Path) -> None:
    settings = Settings(auth_mode="local", data_dir=tmp_path / "data")
    create_ui(settings, client_factory=AuthFactory())
    assert (tmp_path / "data" / "auth_secret").exists()


@pytest.mark.req("AUTH-06")
def test_a_ui_only_process_without_a_secret_refuses_to_start(tmp_path: Path) -> None:
    settings = Settings(
        auth_mode="local",
        data_dir=blocked_data_dir(tmp_path),
        ui_api_url="http://api.example/api/v1",
    )
    options = StartOptions(api=False, ui=True, services=False)
    with pytest.raises(StartupError, match="HELIOPUDDLE_AUTH_SECRET_KEY") as info:
        plan_servers(settings, None, options)
    assert info.value.exit_code == 2


@pytest.mark.req("AUTH-06")
def test_a_ui_only_process_with_the_secret_starts(tmp_path: Path) -> None:
    settings = make_settings(
        auth_mode="local",
        data_dir=blocked_data_dir(tmp_path),
        auth_secret_key=SECRET,
        ui_api_url="http://api.example/api/v1",
    )
    specs = plan_servers(settings, None, StartOptions(api=False, ui=True, services=False))
    assert [s.name for s in specs] == ["ui"]

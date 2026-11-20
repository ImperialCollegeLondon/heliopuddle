"""``heliopuddle user`` error reporting, client use and docs."""

from __future__ import annotations

import inspect
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from heliopuddle import cli_user
from heliopuddle.cli import app
from heliopuddle.client import HelioPuddleClient
from heliopuddle.server.api import create_api
from tests.local_auth import local_context, login
from tests.unit import _cli_user_helpers as _fixtures
from tests.unit._cli_user_helpers import run, runner

_clean_env = _fixtures._clean_env
api = _fixtures.api
lctx = _fixtures.lctx


def _cli_env_vars() -> set[str]:
    import typer.main

    command = typer.main.get_command(app)
    add = command.commands["user"].commands["add"]  # type: ignore[attr-defined]
    found = {p.envvar for p in add.params if p.envvar}
    return {str(e) for e in found} | {"HELIOPUDDLE_PASSWORD", "HELIOPUDDLE_NEW_PASSWORD"}


@pytest.mark.req("AUTH-14")
def test_the_last_admin_guard_is_reported(api: TestClient) -> None:
    token = login(api, "root")
    for args in (
        ("disable", "root"),
        ("delete", "root", "--yes"),
        ("set-roles", "root", "--roles", ""),
    ):
        r = run(*args, "--token", token)
        assert r.exit_code == 1, args
        assert r.stderr.startswith("Error: ") and "last active hp-admin" in r.stderr


@pytest.mark.req("AUTH-14")
def test_header_mode_says_users_are_managed_elsewhere(
    tmp_path: Path, config_dir: Path, migrated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = local_context(tmp_path, config_dir, migrated_db, auth_mode="header")
    with TestClient(create_api(ctx)) as http:
        monkeypatch.setattr(
            cli_user,
            "_client_factory",
            lambda api_url, *, token, username, password: HelioPuddleClient(
                api_url, None, token=token, http_client=http
            ),
        )
        for args in (["list"], ["show", "ann"], ["delete", "ann", "--yes"]):
            r = run(*args, "--token", "anything")
            assert r.exit_code == 1, args
            assert "users are managed elsewhere" in r.stderr
            assert "'header'" in r.stderr
    ctx.dispose()


@pytest.mark.req("AUTH-14")
def test_an_unreachable_api_is_a_clean_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    http = httpx.Client(base_url="http://127.0.0.1:9", transport=httpx.MockTransport(refuse))
    monkeypatch.setattr(
        cli_user,
        "_client_factory",
        lambda api_url, *, token, username, password: HelioPuddleClient(
            api_url, None, token=token, http_client=http
        ),
    )
    r = run("list", "--token", "t", "--api-url", "http://127.0.0.1:9/api/v1")
    assert r.exit_code == 1
    assert r.stderr.startswith("Error: cannot reach the API at http://127.0.0.1:9/api/v1")
    assert "Traceback" not in r.output


@pytest.mark.req("AUTH-14")
def test_the_default_factory_builds_a_real_client() -> None:
    client = cli_user._client_factory(
        "http://127.0.0.1:1/api/v1", token="secret-token-xyz", username=None, password=None
    )
    assert isinstance(client, HelioPuddleClient)
    assert "secret-token-xyz" not in repr(client)
    client.close()


@pytest.mark.req("AUTH-14")
def test_the_module_only_uses_the_client() -> None:
    source = inspect.getsource(cli_user)
    for forbidden in (
        "sqlalchemy",
        "heliopuddle.server.database",
        "heliopuddle.server.models",
        "heliopuddle.server.context",
        "heliopuddle.cli ",
    ):
        assert forbidden not in source.replace("heliopuddle.cli_user", "")


@pytest.mark.req("AUTH-14")
def test_the_settings_table_spells_out_the_environment_variables_the_cli_reads() -> None:
    from pathlib import Path

    table = (Path(__file__).parents[2] / "docs" / "design" / "architecture.md").read_text()
    variables = _cli_env_vars()
    assert {"HELIOPUDDLE_API_URL", "HELIOPUDDLE_TOKEN", "HELIOPUDDLE_USERNAME"} <= variables
    for name in variables:
        assert f"`{name}`" in table, f"{name} is read by the CLI but not documented"
    for wrong in ("HELIOPUDDLE_API_USERNAME", "HELIOPUDDLE_API_PASSWORD", "HELIOPUDDLE_API_TOKEN"):
        assert wrong not in table  # leaves the CLI unauthenticated if someone uses it


@pytest.mark.req("AUTH-14")
def test_the_quick_starts_say_who_to_log_in_as_before_creating_a_user() -> None:
    from pathlib import Path

    root = Path(__file__).parents[2]
    for doc in ("README.md", "docs/design/deployment.md"):
        text = (root / doc).read_text()
        before_add = text.split("user add alice", 1)[0]
        assert "HELIOPUDDLE_USERNAME=admin" in before_add[-600:], doc
        assert "user add alice --roles hp-reader,hp-writer --username" not in text  # confusing


@pytest.mark.req("AUTH-14")
def test_missing_credentials_say_which_account_to_name() -> None:
    r = runner.invoke(
        app, ["user", "list"], env={"HELIOPUDDLE_USERNAME": "", "HELIOPUDDLE_TOKEN": ""}
    )
    assert r.exit_code == 2
    assert "give credentials" in r.stderr
    assert "not the user you are creating" in r.stderr
    assert "HELIOPUDDLE_USERNAME" in r.stderr

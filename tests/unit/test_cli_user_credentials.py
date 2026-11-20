"""``heliopuddle user`` credentials and password handling."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from typer.main import get_command

from heliopuddle.cli import app
from heliopuddle.server.auth.roles import ROLE_ADMIN
from tests.local_auth import PASSWORD, login
from tests.unit import _cli_user_helpers as _fixtures
from tests.unit._cli_user_helpers import NEW, as_root, run, runner

_clean_env = _fixtures._clean_env
api = _fixtures.api
lctx = _fixtures.lctx


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def plain(output: str) -> str:
    """Help text without colour codes (CI sets ``FORCE_COLOR``) and with spaces collapsed."""
    return re.sub(r"\s+", " ", _ANSI.sub("", output))


@pytest.mark.req("AUTH-14")
def test_credentials_from_a_hidden_prompt(api: TestClient) -> None:
    r = run("list", "--username", "root", input=PASSWORD + "\n")
    assert r.exit_code == 0, r.output
    assert "ann" in r.stdout
    assert PASSWORD not in r.output  # the prompt is hidden: the input is not echoed
    assert "Password" in r.output


@pytest.mark.req("AUTH-14")
def test_credentials_from_password_stdin(api: TestClient) -> None:
    r = as_root("list", input=PASSWORD + "\n")
    assert r.exit_code == 0, r.output
    assert "Password:" not in r.output  # no prompt
    assert "root" in r.stdout


@pytest.mark.req("AUTH-14")
def test_credentials_from_env_username_and_password(api: TestClient) -> None:
    env = {"HELIOPUDDLE_USERNAME": "root", "HELIOPUDDLE_PASSWORD": PASSWORD}
    r = run("list", env=env)
    assert r.exit_code == 0, r.output
    assert "ann" in r.stdout


@pytest.mark.req("AUTH-14")
def test_credentials_from_a_token_option_or_env(api: TestClient) -> None:
    token = login(api, "root")
    r = run("list", "--token", token)
    assert r.exit_code == 0, r.output
    assert "ann" in r.stdout
    assert token not in r.output
    r = run("list", env={"HELIOPUDDLE_TOKEN": token})
    assert r.exit_code == 0, r.output


@pytest.mark.req("AUTH-14")
def test_api_url_option_env_and_default(api: TestClient) -> None:
    token = login(api, "root")
    seen: list[str] = api.seen_urls  # type: ignore[attr-defined]
    run("list", "--token", token)
    run("list", "--token", token, "--api-url", "http://hp.example/api/v1")
    run("list", env={"HELIOPUDDLE_TOKEN": token, "HELIOPUDDLE_API_URL": "http://env.example/api"})
    assert seen == [
        "http://127.0.0.1:8080/api/v1",
        "http://hp.example/api/v1",
        "http://env.example/api",
    ]


@pytest.mark.req("AUTH-14")
def test_missing_or_conflicting_credentials_are_usage_errors(api: TestClient) -> None:
    r = run("list")
    assert r.exit_code == 2
    assert r.stderr.startswith("Error: give credentials")
    r = run("list", "--token", "t", "--username", "root")
    assert r.exit_code == 2
    assert "either --token or --username" in r.stderr
    r = run("list", "--token", "t", "--password-stdin")
    assert r.exit_code == 2
    r = run("list", "--username", "root", "--password-stdin", input="")
    assert r.exit_code == 2
    assert "first line of stdin" in r.stderr


@pytest.mark.req("AUTH-14")
def test_wrong_password_and_bad_token_say_authentication_failed(api: TestClient) -> None:
    r = run("list", "--username", "root", "--password-stdin", input="not the password\n")
    assert r.exit_code == 1
    assert r.stderr.startswith("Error: authentication failed")
    assert "not the password" not in r.output
    r = run("list", "--token", "garbage")
    assert r.exit_code == 1
    assert r.stderr.startswith("Error: authentication failed")
    assert "garbage" not in r.output


@pytest.mark.req("AUTH-14")
def test_a_non_admin_is_told_which_role_is_needed(api: TestClient) -> None:
    r = run("list", "--username", "ann", "--password-stdin", input=PASSWORD + "\n")
    assert r.exit_code == 1
    assert "permission denied" in r.stderr
    assert ROLE_ADMIN in r.stderr


@pytest.mark.req("AUTH-14")
def test_no_command_accepts_a_password_on_argv() -> None:
    root = get_command(app)
    user = root.commands["user"]  # type: ignore[attr-defined]
    assert set(user.commands) == {  # type: ignore[attr-defined]
        "add",
        "list",
        "show",
        "set-password",
        "set-roles",
        "disable",
        "enable",
        "delete",
    }
    for name, command in user.commands.items():  # type: ignore[attr-defined]
        for param in command.params:
            options = getattr(param, "opts", [])
            assert not any("password" in o and "stdin" not in o for o in options), (name, options)
            assert param.name is None or "password" not in param.name or "stdin" in param.name


@pytest.mark.req("AUTH-14")
def test_a_password_option_is_rejected(api: TestClient) -> None:
    for option in ("--password", "--new-password"):
        r = as_root("add", "bob", option, NEW, input=PASSWORD + "\n")
        assert r.exit_code == 2
        assert "No such option" in r.output
        assert NEW not in r.output or "No such option" in r.output


@pytest.mark.req("AUTH-14")
def test_help_documents_the_password_sources() -> None:
    r = runner.invoke(app, ["user", "add", "--help"], terminal_width=200)
    assert r.exit_code == 0
    out = plain(r.output)
    for text in (
        "HELIOPUDDLE_NEW_PASSWORD",
        "--new-password-stdin",
        "--password-stdin",
        "HELIOPUDDLE_API_URL",
        "HELIOPUDDLE_TOKEN",
        "HELIOPUDDLE_USERNAME",
    ):
        assert text in out, text
    top = runner.invoke(app, ["user", "--help"], terminal_width=200)
    assert "HELIOPUDDLE_PASSWORD" in plain(top.output)

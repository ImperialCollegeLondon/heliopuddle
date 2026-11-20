"""Shared runner, fixtures and helpers for the user CLI tests."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from heliopuddle import cli_user
from heliopuddle.cli import app
from heliopuddle.client import HelioPuddleClient
from heliopuddle.server.api import create_api
from heliopuddle.server.auth.roles import ROLE_ADMIN, ROLE_READER
from heliopuddle.server.context import AppContext
from tests.local_auth import add_user, local_context

NEW = "a brand new password"


runner = CliRunner()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "HELIOPUDDLE_API_URL",
        "HELIOPUDDLE_TOKEN",
        "HELIOPUDDLE_USERNAME",
        "HELIOPUDDLE_PASSWORD",
        "HELIOPUDDLE_NEW_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def lctx(tmp_path: Path, config_dir: Path, migrated_db: Path) -> Iterator[AppContext]:
    ctx = local_context(tmp_path, config_dir, migrated_db)
    add_user(ctx, "root", [ROLE_ADMIN])
    add_user(ctx, "ann", [ROLE_READER])
    yield ctx
    ctx.dispose()


@pytest.fixture
def api(lctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The API served in process; the CLI's client factory talks to it."""
    seen: list[str] = []
    with TestClient(create_api(lctx)) as http:

        def factory(
            api_url: str, *, token: str | None, username: str | None, password: str | None
        ) -> HelioPuddleClient:
            seen.append(api_url)
            return HelioPuddleClient(
                api_url, None, token=token, username=username, password=password, http_client=http
            )

        monkeypatch.setattr(cli_user, "_client_factory", factory)
        http.seen_urls = seen  # type: ignore[attr-defined]
        yield http


def run(*args: str, input: str | None = None, env: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
    return runner.invoke(app, ["user", *args], input=input, env=env)


def as_root(*args: str, input: str | None = None, env: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
    """Run a command logged in as root, with the password on stdin."""
    return run(*args, "--username", "root", "--password-stdin", input=input, env=env)

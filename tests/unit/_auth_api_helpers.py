"""Shared fixtures and helpers for the auth API tests."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from heliopuddle.server.api import create_api
from heliopuddle.server.auth.roles import ROLE_READER
from heliopuddle.server.context import AppContext
from tests.local_auth import add_user, local_context


@pytest.fixture
def lctx(tmp_path: Path, config_dir: Path, migrated_db: Path) -> Iterator[AppContext]:
    ctx = local_context(tmp_path, config_dir, migrated_db)
    yield ctx
    ctx.dispose()


@pytest.fixture
def client(lctx: AppContext) -> Iterator[TestClient]:
    add_user(lctx, "Ann", [ROLE_READER])
    with TestClient(create_api(lctx)) as c:
        yield c


def post_token(client: TestClient, **form: str) -> Any:
    return client.post("/auth/token", data=form)


def password_form(username: str, password: str) -> dict[str, str]:
    return {"grant_type": "password", "username": username, "password": password}

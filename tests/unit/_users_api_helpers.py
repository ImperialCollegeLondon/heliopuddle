"""Shared fixtures and helpers for the users API tests."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from heliopuddle.server.api import create_api
from heliopuddle.server.auth.roles import ROLE_ADMIN, ROLE_READER
from heliopuddle.server.context import AppContext
from tests.local_auth import add_user, bearer, local_context, login


@pytest.fixture
def lctx(tmp_path: Path, config_dir: Path, migrated_db: Path) -> Iterator[AppContext]:
    ctx = local_context(tmp_path, config_dir, migrated_db)
    yield ctx
    ctx.dispose()


@pytest.fixture
def client(lctx: AppContext) -> Iterator[TestClient]:
    add_user(lctx, "Root", [ROLE_ADMIN])
    add_user(lctx, "reader", [ROLE_READER])
    with TestClient(create_api(lctx)) as c:
        yield c


@pytest.fixture
def admin(client: TestClient) -> dict[str, str]:
    return bearer(login(client, "root"))


def body(r) -> dict:  # type: ignore[no-untyped-def]
    assert r.status_code < 300, r.text
    return r.json()  # type: ignore[no-any-return]

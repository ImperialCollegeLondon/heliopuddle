"""Helpers for tests of ``local`` auth mode (AUTH-03 to AUTH-12)."""

from __future__ import annotations

import shutil
from collections.abc import Iterable
from pathlib import Path

from fastapi.testclient import TestClient

from heliopuddle.server.auth.passwords import hash_password
from heliopuddle.server.auth.secret import get_secret
from heliopuddle.server.auth.tokens import create_token
from heliopuddle.server.context import AppContext, set_context
from heliopuddle.server.models import users as users_db
from heliopuddle.settings import Settings

PASSWORD = "correct horse battery"


def local_context(
    tmp_path: Path, config_dir: Path, migrated_db: Path, **settings: object
) -> AppContext:
    """A migrated, config-synced context in ``local`` auth mode (no users yet)."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(migrated_db, data_dir / "heliopuddle.db")
    kwargs: dict[str, object] = {"auth_mode": "local", **settings}
    s = Settings(config_dir=config_dir, data_dir=data_dir, **kwargs)  # type: ignore[arg-type]
    ctx = AppContext.from_settings(s)
    ctx.sync_config()
    set_context(ctx)
    return ctx


def add_user(
    ctx: AppContext,
    username: str,
    roles: list[str] | None = None,
    password: str = PASSWORD,
    *,
    is_active: bool = True,
) -> None:
    """Insert a user directly."""
    with ctx.write_session() as session:
        users_db.create_user(
            session, username, hash_password(password), roles or [], is_active=is_active
        )


def issue_token(
    ctx: AppContext,
    username: str,
    roles: Iterable[str] = (),
    ttl: int = 3600,
    *,
    now: float | None = None,
    user_id: int | None = None,
    version: int | None = None,
) -> str:
    """A signed token for ``username``, bound to the user's current id and credentials version.

    ``user_id`` and ``version`` override what is bound (a missing user gets an id no row has).
    """
    with ctx.sessionmaker() as session:
        user = users_db.get_user_by_username(session, username)
        bound_id = user_id if user_id is not None else (user.id if user else 999_999)
        bound_version = (
            version if version is not None else (users_db.credentials_version(user) if user else 1)
        )
    return create_token(
        get_secret(ctx.settings),
        username,
        list(roles),
        ttl,
        user_id=bound_id,
        version=bound_version,
        now=now,
    )


def login(client: TestClient, username: str, password: str = PASSWORD) -> str:
    """Log in through ``POST /auth/token`` and return the access token."""
    r = client.post(
        "/auth/token", data={"grant_type": "password", "username": username, "password": password}
    )
    assert r.status_code == 200, r.text
    return str(r.json()["access_token"])


def bearer(token: str) -> dict[str, str]:
    """An ``Authorization`` header."""
    return {"Authorization": f"Bearer {token}"}

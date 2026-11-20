"""Shared Postgres fixtures and helpers for the local user tests."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from heliopuddle.server.auth.passwords import hash_password
from heliopuddle.server.context import AppContext, set_context
from heliopuddle.server.database.migrate import (
    upgrade_to_head,
)
from heliopuddle.server.models import users as users_db
from heliopuddle.settings import Settings

from .conftest import make_project_dirs

PASSWORD = "correct horse battery"


JOIN_TIMEOUT_S = 120


def make_ctx(url: str, root: Path, **settings: object) -> AppContext:
    """A local-mode context on ``url`` with a new engine; ``root`` holds the config and data dirs.

    The directories are created on the first call for a ``root`` and shared by later ones,
    like replicas of one deployment.
    """
    if not (root / "config").exists():
        make_project_dirs(root)
    kwargs: dict[str, object] = {"auth_mode": "local", "database_url": url, **settings}
    return AppContext.from_settings(
        Settings(config_dir=root / "config", data_dir=root / "data", **kwargs)  # type: ignore[arg-type]
    )


@pytest.fixture
def pg_ctx(postgres_url: str, tmp_path: Path) -> Iterator[AppContext]:
    """A migrated, config-synced local-mode context on a fresh Postgres database."""
    ctx = make_ctx(postgres_url, tmp_path)
    upgrade_to_head(ctx.engine)
    ctx.sync_config()
    set_context(ctx)
    yield ctx
    set_context(None)
    ctx.dispose()


def run_threads(target: Callable[[int], None], n: int) -> None:
    """Run ``target(i)`` in ``n`` threads released together; fail on errors or a hang."""
    errors: list[BaseException] = []
    start = threading.Barrier(n)

    def run(i: int) -> None:
        try:
            start.wait()
            target(i)
        except BaseException as e:  # reported below
            errors.append(e)

    workers = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(JOIN_TIMEOUT_S)
    assert not any(w.is_alive() for w in workers), "a worker is stuck (deadlock?)"
    assert not errors, errors


def add_user(ctx: AppContext, name: str, roles: list[str], *, active: bool = True) -> None:
    with ctx.write_session() as session:
        users_db.create_user(session, name, hash_password(PASSWORD), roles, is_active=active)

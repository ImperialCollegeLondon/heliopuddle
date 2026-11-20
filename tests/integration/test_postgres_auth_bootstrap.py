"""Concurrent first starts create exactly one admin on Postgres (AUTH-02, AUTH-03)."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from heliopuddle.server.auth.bootstrap import ensure_admin
from heliopuddle.server.auth.roles import ROLE_ADMIN
from heliopuddle.server.database.migrate import (
    upgrade_to_head,
)
from heliopuddle.server.models import users as users_db
from tests.integration._postgres_auth_helpers import JOIN_TIMEOUT_S, PASSWORD, make_ctx, run_threads

from .conftest import base_env


@pytest.mark.req("AUTH-03")
def test_concurrent_first_starts_create_exactly_one_admin_on_postgres(
    postgres_url: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Several replicas (each with its own engine and pool) start at once."""
    first = make_ctx(postgres_url, tmp_path)
    upgrade_to_head(first.engine)
    contexts = [first] + [make_ctx(postgres_url, tmp_path) for _ in range(5)]
    try:
        with caplog.at_level("WARNING", logger="heliopuddle"):
            run_threads(lambda i: ensure_admin(contexts[i]), len(contexts))
        with first.sessionmaker() as session:
            users = users_db.list_users(session)
            assert [(u.username, u.roles, u.is_active) for u in users] == [
                ("admin", [ROLE_ADMIN], True)
            ]
        created = [r for r in caplog.records if "created the first admin" in r.getMessage()]
        assert len(created) == 1  # only the winner logs the generated password
    finally:
        for ctx in contexts:
            ctx.dispose()


@pytest.mark.req("AUTH-03")
def test_concurrent_first_start_processes_create_exactly_one_admin(
    postgres_url: str, tmp_path: Path
) -> None:
    """Separate OS processes race on ``ensure_admin`` against one database."""
    ctx = make_ctx(postgres_url, tmp_path)
    upgrade_to_head(ctx.engine)
    script = (
        "import time, sys\n"
        "from heliopuddle.server.context import AppContext\n"
        "from heliopuddle.server.auth.bootstrap import ensure_admin\n"
        "ctx = AppContext.from_settings()\n"
        "while time.time() < float(sys.argv[1]):\n"
        "    time.sleep(0.005)\n"
        "ensure_admin(ctx)\n"
        "ctx.dispose()\n"
    )
    env = base_env(
        HELIOPUDDLE_AUTH_MODE="local",
        HELIOPUDDLE_DATABASE_URL=postgres_url,
        HELIOPUDDLE_CONFIG_DIR=str(tmp_path / "config"),
        HELIOPUDDLE_DATA_DIR=str(tmp_path / "data"),
    )
    go_at = str(time.time() + 6.0)  # every process imports first, then they start together
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", script, go_at],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for _ in range(4)
    ]
    outputs = []
    try:
        for p in procs:
            out, _ = p.communicate(timeout=JOIN_TIMEOUT_S)
            outputs.append(out)
            assert p.returncode == 0, out
        with ctx.sessionmaker() as session:
            assert [u.username for u in users_db.list_users(session)] == ["admin"]
        assert sum("created the first admin" in o for o in outputs) == 1, outputs
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
        ctx.dispose()


@pytest.mark.req("AUTH-03")
def test_concurrent_first_starts_with_different_admin_usernames_create_one_admin(
    postgres_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Different usernames never conflict on the unique index, so only the startup advisory
    lock stops each replica from creating its own admin. Hashing is slowed after the count,
    which would let every replica through without the lock."""
    from heliopuddle.server.auth import bootstrap

    real_hash = bootstrap.hash_password

    def slow_hash(password: str) -> str:
        time.sleep(0.4)
        return real_hash(password)

    monkeypatch.setattr(bootstrap, "hash_password", slow_hash)
    first = make_ctx(postgres_url, tmp_path, admin_username="admin0", admin_password=PASSWORD)
    upgrade_to_head(first.engine)
    contexts = [first] + [
        make_ctx(postgres_url, tmp_path, admin_username=f"admin{i}", admin_password=PASSWORD)
        for i in range(1, 5)
    ]
    try:
        run_threads(lambda i: ensure_admin(contexts[i]), len(contexts))
        with first.sessionmaker() as session:
            users = users_db.list_users(session)
        assert len(users) == 1, [u.username for u in users]
        assert users[0].roles == [ROLE_ADMIN]
    finally:
        for ctx in contexts:
            ctx.dispose()

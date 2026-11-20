"""``heliopuddle db upgrade`` and migrations on ``server start``."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from heliopuddle.cli import app
from heliopuddle.server import start as start_mod
from heliopuddle.server.database import migrate
from heliopuddle.server.database.migrate import current_revision, head_revision
from tests.unit import _cli_helpers as _fixtures
from tests.unit._cli_helpers import FakeRuntime, db_url, runner

env = _fixtures.env
fake = _fixtures.fake


@pytest.mark.req("DB-04")
def test_db_upgrade_on_fresh_db_exits_0(env: dict[str, str]) -> None:
    result = runner.invoke(app, ["db", "upgrade"])

    assert result.exit_code == 0, result.output
    assert current_revision(db_url(env)) == head_revision()
    assert f"at revision {head_revision()}" in result.output


@pytest.mark.req("DB-04")
def test_db_upgrade_at_head_is_a_no_op(env: dict[str, str]) -> None:
    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
    result = runner.invoke(app, ["db", "upgrade"])

    assert result.exit_code == 0, result.output
    assert current_revision(db_url(env)) == head_revision()


@pytest.mark.req("DB-04")
def test_db_upgrade_needs_no_project_config(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HELIOPUDDLE_CONFIG_DIR", str(tmp_path / "missing"))

    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0


@pytest.mark.req("DB-04")
def test_db_upgrade_failing_migration_exits_non_zero(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("migration 0001 exploded")

    monkeypatch.setattr(migrate.command, "upgrade", boom)

    result = runner.invoke(app, ["db", "upgrade"])

    assert result.exit_code == 1
    assert "Database migration failed" in result.output
    assert "migration 0001 exploded" in result.output
    assert current_revision(db_url(env)) is None


@pytest.mark.req("DB-04")
def test_db_upgrade_hides_the_database_password(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HELIOPUDDLE_DATABASE_URL", "postgresql+nodriver://hp:s3cret@db/hp")

    result = runner.invoke(app, ["db", "upgrade"])

    assert result.exit_code == 1
    assert "s3cret" not in result.output
    assert "hp:***@db/hp" in result.output


@pytest.mark.req("DB-04")
@pytest.mark.req("DEP-02")
def test_server_start_migrates_by_default(env: dict[str, str], fake: FakeRuntime) -> None:
    result = runner.invoke(app, ["server", "start"])

    assert result.exit_code == 0, result.output
    assert current_revision(db_url(env)) == head_revision()
    assert fake.prefect_started == fake.prefect_stopped == 1
    assert fake.services_started == fake.services_stopped == 1
    assert [s.name for s in fake.servers] == ["api+ui"]
    # config was synced into the DB before the servers started
    ctx = fake.contexts[0]
    with ctx.sessionmaker() as session:
        from heliopuddle.server import models

        assert models.get_project(session, "demo") is not None


@pytest.mark.req("DB-04")
def test_no_migrate_on_empty_schema_fails_clearly(env: dict[str, str], fake: FakeRuntime) -> None:
    result = runner.invoke(app, ["server", "start", "--no-migrate"])

    assert result.exit_code == 1
    assert "not at head" in result.output
    assert "heliopuddle db upgrade" in result.output
    assert fake.prefect_started == 0
    assert current_revision(db_url(env)) is None


@pytest.mark.req("DB-04")
def test_no_migrate_on_old_schema_fails_clearly(env: dict[str, str], fake: FakeRuntime) -> None:
    path = Path(env["HELIOPUDDLE_DATA_DIR"]) / "heliopuddle.db"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)")
        conn.execute("INSERT INTO alembic_version VALUES ('0000_older')")

    result = runner.invoke(app, ["server", "start", "--no-migrate"])

    assert result.exit_code == 1
    assert "0000_older" in result.output
    assert f"not at head {head_revision()}" in result.output


@pytest.mark.req("DB-04")
def test_no_migrate_at_head_starts(env: dict[str, str], fake: FakeRuntime) -> None:
    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0

    result = runner.invoke(app, ["server", "start", "--no-migrate"])

    assert result.exit_code == 0, result.output
    assert fake.prefect_started == 1


@pytest.mark.req("DB-04")
def test_server_start_failing_migration_stops_startup(
    env: dict[str, str], fake: FakeRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("bad migration")

    monkeypatch.setattr(migrate.command, "upgrade", boom)

    result = runner.invoke(app, ["server", "start"])

    assert result.exit_code == 1
    assert "Database migration failed" in result.output
    assert "bad migration" in result.output
    assert fake.prefect_started == 0


@pytest.mark.req("DB-04")
def test_unreadable_schema_revision_fails_clearly(
    env: dict[str, str], fake: FakeRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any) -> bool:
        raise OSError("disk on fire")

    monkeypatch.setattr(start_mod, "is_at_head", broken)

    result = runner.invoke(app, ["server", "start", "--no-migrate"])

    assert result.exit_code == 1
    assert "Cannot read the schema revision" in result.output


def test_config_sync_failure_stops_startup(
    env: dict[str, str], fake: FakeRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self: Any) -> None:
        raise RuntimeError("sync broke")

    monkeypatch.setattr(start_mod.AppContext, "sync_config", broken)

    result = runner.invoke(app, ["server", "start"])

    assert result.exit_code == 1
    assert "Syncing project config" in result.output

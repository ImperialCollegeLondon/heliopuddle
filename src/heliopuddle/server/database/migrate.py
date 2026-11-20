"""Run alembic migrations programmatically (DB-01, DB-04).

The migration scripts are packaged in ``heliopuddle/server/database/migrations``
so that they work from an installed wheel (for example in Docker); no
``alembic.ini`` is read.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine, text
from sqlalchemy.engine import URL

from heliopuddle.server.database.engine import WRITE_OPTION, create_engine_for
from heliopuddle.server.database.orm import Base

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# Postgres advisory lock key taken while migrating or syncing config, so that
# several processes starting against one database do not race ("hpschema").
SCHEMA_LOCK_KEY = 0x6870_7363_6865_6D61


def lock_schema(connection: Connection) -> None:
    """Serialise schema changes across processes sharing a Postgres database.

    Takes a transaction-level advisory lock, released when the transaction
    ends. On SQLite this does nothing: its write transactions already
    serialise.

    Args:
        connection: a connection inside the transaction to protect.
    """
    if connection.dialect.name == "postgresql":
        connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": SCHEMA_LOCK_KEY})


def alembic_config(connection: Connection | None = None) -> Config:
    """Return an alembic config for the packaged migrations.

    Args:
        connection: if given, migrations and checks run on this connection.

    Returns:
        A :class:`alembic.config.Config` usable with :mod:`alembic.command`.
    """
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    if connection is not None:
        cfg.attributes["connection"] = connection
    return cfg


@contextmanager
def _connect(url: str | URL | Engine) -> Iterator[Connection]:
    """A write transaction for migrating.

    On SQLite, foreign keys are switched off for the transaction (they can only
    be switched outside one), as SQLite's documented procedure for table
    rebuilds requires: dropping the old table would otherwise fail while other
    rows reference it. Before the commit ``PRAGMA foreign_key_check`` must find
    no violation; afterwards foreign keys are switched on again.
    """
    engine = url if isinstance(url, Engine) else create_engine_for(url)
    try:
        with engine.connect() as conn:
            sqlite = conn.dialect.name == "sqlite"
            if sqlite:
                conn.connection.driver_connection.execute("PRAGMA foreign_keys=OFF")  # type: ignore[union-attr]
            try:
                conn.execution_options(**{WRITE_OPTION: True})
                with conn.begin():
                    yield conn
                    if sqlite:
                        _check_foreign_keys(conn)
            finally:
                if sqlite:
                    conn.connection.driver_connection.execute("PRAGMA foreign_keys=ON")  # type: ignore[union-attr]
    finally:
        if engine is not url:
            engine.dispose()


def _check_foreign_keys(conn: Connection) -> None:
    violations = conn.exec_driver_sql("PRAGMA foreign_key_check").all()
    if violations:
        raise RuntimeError(f"migration left foreign key violations: {violations[:5]}")


def head_revision() -> str | None:
    """Return the head revision of the packaged migrations."""
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def current_revision(url: str | URL | Engine) -> str | None:
    """Return the database's current revision, or None if it has none.

    Args:
        url: database URL (or an existing engine).
    """
    with _connect(url) as conn:
        return MigrationContext.configure(conn).get_current_revision()


def upgrade_to_head(url: str | URL | Engine) -> None:
    """Run every pending migration (``alembic upgrade head``) in one transaction.

    Args:
        url: database URL (or an existing engine). A SQLite file's directory
            is created if needed.

    Raises:
        Exception: whatever the failing migration raised; nothing is committed.
    """
    with _connect(url) as conn:
        lock_schema(conn)
        command.upgrade(alembic_config(conn), "head")


def downgrade_to(url: str | URL | Engine, revision: str) -> None:
    """Undo migrations down to ``revision`` (``alembic downgrade``) in one transaction.

    Args:
        url: database URL (or an existing engine).
        revision: the revision to end at (``base`` for none).
    """
    with _connect(url) as conn:
        lock_schema(conn)
        command.downgrade(alembic_config(conn), revision)


def is_at_head(url: str | URL | Engine) -> bool:
    """Return True if the database schema is at the packaged head revision.

    Args:
        url: database URL (or an existing engine).
    """
    return current_revision(url) == head_revision()


def schema_diffs(url: str | URL | Engine) -> list[Any]:
    """Compare the database schema with the ORM models (``alembic check``).

    Args:
        url: database URL (or an existing engine).

    Returns:
        Alembic autogenerate differences; empty when the schema matches.
    """
    with _connect(url) as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        return list(compare_metadata(ctx, Base.metadata))

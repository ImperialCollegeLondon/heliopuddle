"""Alembic environment for heliopuddle.

Driven programmatically by :mod:`heliopuddle.server.database.migrate`; no
``alembic.ini`` is needed. Pass an open connection in
``config.attributes["connection"]`` or set ``sqlalchemy.url``.
"""

from __future__ import annotations

from typing import Any, Literal

from alembic import context
from sqlalchemy.engine import Connection

from heliopuddle.server.database.engine import create_engine_for
from heliopuddle.server.database.orm import Base, UTCDateTime

config = context.config
target_metadata = Base.metadata


def render_item(type_: str, obj: Any, autogen_context: Any) -> str | Literal[False]:
    """Render custom column types as plain SQLAlchemy types in revision files."""
    if type_ == "type" and isinstance(obj, UTCDateTime):
        return "sa.DateTime(timezone=True)"
    return False


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        render_item=render_item,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations on the given connection, or on a new engine for the URL."""
    connection = config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    url = config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError("set config.attributes['connection'] or sqlalchemy.url")
    engine = create_engine_for(url)
    try:
        with engine.begin() as conn:
            _run(conn)
    finally:
        engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("offline (--sql) migrations are not supported")
run_migrations_online()

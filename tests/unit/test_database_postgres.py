"""Postgres engine settings and the schema lock, without a Postgres server (DB-01b).

The migrations themselves run on a real Postgres in
``tests/integration/test_postgres.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import QueuePool, text

from heliopuddle.server.database.engine import (
    POSTGRES_POOL_RECYCLE_S,
    create_engine_for,
)
from heliopuddle.server.database.migrate import SCHEMA_LOCK_KEY, lock_schema


@pytest.mark.req("DB-01b")
def test_postgres_engine_uses_psycopg_with_a_checked_pool() -> None:
    engine = create_engine_for("postgresql+psycopg://hp:pw@db.invalid:5432/hp")
    try:
        assert engine.dialect.name == "postgresql"
        assert engine.dialect.driver == "psycopg"
        assert isinstance(engine.pool, QueuePool)
        assert engine.pool._pre_ping is True
        assert engine.pool._recycle == POSTGRES_POOL_RECYCLE_S
    finally:
        engine.dispose()


@pytest.mark.req("DB-01b")
def test_plain_postgresql_url_gets_the_psycopg_driver() -> None:
    engine = create_engine_for("postgresql://hp@db.invalid/hp")
    try:
        assert engine.dialect.driver == "psycopg"
    finally:
        engine.dispose()


@pytest.mark.req("DB-01b")
def test_postgres_engine_kwargs_override_the_defaults() -> None:
    engine = create_engine_for("postgresql+psycopg://hp@db.invalid/hp", pool_recycle=60)
    try:
        assert engine.pool._recycle == 60
    finally:
        engine.dispose()


class _FakeConnection:
    def __init__(self, dialect: str) -> None:
        self.dialect = type("Dialect", (), {"name": dialect})()
        self.calls: list[tuple[str, Any]] = []

    def execute(self, statement: Any, params: Any = None) -> None:
        self.calls.append((str(statement), params))


@pytest.mark.req("DB-01b")
def test_lock_schema_takes_a_transaction_advisory_lock_on_postgres() -> None:
    conn = _FakeConnection("postgresql")

    lock_schema(conn)  # type: ignore[arg-type]

    assert conn.calls == [("SELECT pg_advisory_xact_lock(:key)", {"key": SCHEMA_LOCK_KEY})]


@pytest.mark.req("DB-01b")
def test_lock_schema_is_a_no_op_on_sqlite() -> None:
    engine = create_engine_for("sqlite://")
    try:
        with engine.begin() as conn:
            lock_schema(conn)
            assert conn.execute(text("SELECT 1")).scalar() == 1
    finally:
        engine.dispose()

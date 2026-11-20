"""Engine and session helpers for SQLite and Postgres.

On SQLite every connection runs in WAL mode with foreign keys on. Read
transactions are plain deferred ``BEGIN`` transactions, so readers never take
the write lock. Write transactions, opened with :func:`write_session`, start
with ``BEGIN IMMEDIATE`` so that writers are serialised (SQLite's equivalent of
the row locks taken with ``SELECT ... FOR UPDATE`` on Postgres). This uses
SQLAlchemy's documented pysqlite recipe: pysqlite's own implicit ``BEGIN`` is
disabled and SQLAlchemy emits the ``BEGIN``.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Any

from sqlalchemy import Engine, create_engine, event, make_url
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)

# Seconds a SQLite writer waits for the database lock before failing.
SQLITE_BUSY_TIMEOUT = 30

# Connection execution option that marks a write transaction (BEGIN IMMEDIATE on SQLite).
WRITE_OPTION = "heliopuddle_write"


def is_sqlite(url: str | URL) -> bool:
    """Return True if ``url`` is a SQLite URL."""
    return make_url(url).get_backend_name() == "sqlite"


def sqlite_module() -> ModuleType:
    """Return the SQLite DB-API module: ``pysqlite3`` (a bundled, current SQLite) if installed.

    hp depends on ``pysqlite3`` where the package ships wheels; elsewhere the
    standard library's ``sqlite3`` (the system SQLite) is used.
    """
    try:
        return importlib.import_module("pysqlite3.dbapi2")
    except ImportError:
        return importlib.import_module("sqlite3")


def _sqlite_file(url: URL) -> Path | None:
    database = url.database
    if not database or database == ":memory:" or database.startswith("file:"):
        return None
    return Path(database)


def create_engine_for(url: str | URL, **kwargs: Any) -> Engine:
    """Create an engine for ``url``, configured for heliopuddle.

    For Postgres: uses psycopg (v3), even for a plain ``postgresql://`` URL,
    with a pre-pinged pool whose connections are recycled after 30 minutes.

    For SQLite: creates the database file's parent directory, sets WAL mode,
    turns foreign keys on, starts read transactions with a deferred ``BEGIN``
    and write transactions (see :func:`write_session`) with ``BEGIN IMMEDIATE``.

    Args:
        url: a SQLAlchemy database URL.
        **kwargs: extra arguments for :func:`sqlalchemy.create_engine`.

    Returns:
        The engine.
    """
    sa_url = make_url(url)
    if sa_url.get_backend_name() == "postgresql":
        return _create_postgres_engine(sa_url, **kwargs)
    if not is_sqlite(sa_url):
        return create_engine(sa_url, pool_pre_ping=True, **kwargs)

    db_file = _sqlite_file(sa_url)
    if db_file is not None:
        db_file.parent.mkdir(parents=True, exist_ok=True)
    connect_args = {"timeout": SQLITE_BUSY_TIMEOUT, "check_same_thread": False}
    connect_args.update(kwargs.pop("connect_args", {}))
    kwargs.setdefault("module", sqlite_module())
    engine = create_engine(sa_url, connect_args=connect_args, **kwargs)

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection: Any, _record: Any) -> None:
        # Disable pysqlite's implicit BEGIN; SQLAlchemy emits it in _on_begin.
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        try:
            if db_file is not None:
                cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    @event.listens_for(engine, "begin")
    def _on_begin(conn: Any) -> None:
        write = conn.get_execution_options().get(WRITE_OPTION, False)
        conn.exec_driver_sql("BEGIN IMMEDIATE" if write else "BEGIN")

    return engine


SQLITE_ANALYSIS_LIMIT = 1000


def refresh_statistics(engine: Engine) -> None:
    """Refresh SQLite's query-planner statistics with a bounded ``ANALYZE``.

    Without statistics SQLite guesses at index selectivity and can sort a
    whole table instead of seeking the ``(updated_at, id)`` change-key
    indexes. ``analysis_limit`` keeps it cheap on large databases. Postgres
    keeps its own statistics (autovacuum), so this does nothing there.
    Failures are logged, never raised: statistics only affect speed.
    """
    if engine.dialect.name != "sqlite":
        return
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql(f"PRAGMA analysis_limit={SQLITE_ANALYSIS_LIMIT}")
            conn.exec_driver_sql("ANALYZE")
            conn.commit()
    except Exception:
        logger.warning("refreshing SQLite planner statistics failed", exc_info=True)


def make_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    """Return a session factory bound to ``engine``.

    Objects are not expired on commit, so records stay readable after their
    transaction ends.
    """
    return sessionmaker(engine, expire_on_commit=False)


@contextmanager
def write_session(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Open a write transaction; commit on success, roll back on error.

    On SQLite the transaction starts with ``BEGIN IMMEDIATE``, so it takes the
    database write lock up front (waiting up to :data:`SQLITE_BUSY_TIMEOUT`)
    instead of upgrading a read lock later, which can fail under contention.
    On Postgres it is a plain transaction; row locks come from
    ``SELECT ... FOR UPDATE``.

    Args:
        factory: the session factory.

    Yields:
        A session inside a transaction.
    """
    with factory.begin() as session:
        session.connection(execution_options={WRITE_OPTION: True})
        yield session


# Postgres: seconds after which a pooled connection is replaced (servers and
# proxies drop idle connections); pool_pre_ping also checks each checkout.
POSTGRES_POOL_RECYCLE_S = 1800
POSTGRES_POOL_SIZE = 5
POSTGRES_MAX_OVERFLOW = 10


def _create_postgres_engine(url: URL, **kwargs: Any) -> Engine:
    """Create a Postgres engine using the psycopg (v3) driver.

    A plain ``postgresql://`` URL gets the ``psycopg`` driver, the only one
    installed. Pool defaults can be overridden through ``kwargs``.
    """
    if url.drivername == "postgresql":
        url = url.set(drivername="postgresql+psycopg")
    options: dict[str, Any] = {
        "pool_pre_ping": True,
        "pool_recycle": POSTGRES_POOL_RECYCLE_S,
        "pool_size": POSTGRES_POOL_SIZE,
        "max_overflow": POSTGRES_MAX_OVERFLOW,
    }
    options.update(kwargs)
    return create_engine(url, **options)

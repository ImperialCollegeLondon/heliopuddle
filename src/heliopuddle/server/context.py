"""The process-wide application context: settings, project config and database.

Flows, tasks and the API get the database and project config through
:func:`get_context`, so a task called with only an id (for example
``verify_file``) sees the same config and DB as the API that called it.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from heliopuddle.config import ProjectConfig, StorageLocationConfig, load_projects
from heliopuddle.server.database.engine import (
    create_engine_for,
    make_sessionmaker,
    write_session,
)
from heliopuddle.server.database.migrate import lock_schema
from heliopuddle.server.models import sync_config
from heliopuddle.settings import Settings, get_settings

if TYPE_CHECKING:
    from heliopuddle.server.auth.oidc import OidcProvider


@dataclass
class AppContext:
    """Everything server code needs to reach config and the database.

    Attributes:
        settings: process settings.
        projects: project configs keyed by project name (read-only at runtime).
        engine: the SQLAlchemy engine.
        sessionmaker: session factory bound to ``engine``.
        oidc: the discovered OIDC provider in ``oidc`` auth mode (set at startup or by
            tests), else None.
    """

    settings: Settings
    projects: dict[str, ProjectConfig]
    engine: Engine
    sessionmaker: sessionmaker[Session]
    oidc: OidcProvider | None = None

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> AppContext:
        """Load project config and create the engine.

        With no project files in the config dir, ``default_config.yaml``
        in the data dir (``default`` project, written if missing) is loaded (DEP-10).
        This neither runs migrations nor syncs config into the database; see
        :func:`heliopuddle.server.database.migrate.upgrade_to_head` and
        :meth:`sync_config`.

        Args:
            settings: settings to use; defaults to :func:`get_settings`.

        Raises:
            ConfigError: if the project config is invalid.
        """
        settings = settings or get_settings()
        projects = load_projects(settings.config_dir, default_data_dir=settings.data_dir)
        engine = create_engine_for(settings.resolved_database_url)
        return cls(
            settings=settings,
            projects=projects,
            engine=engine,
            sessionmaker=make_sessionmaker(engine),
        )

    def storage_location_config(self, project: str, name: str) -> StorageLocationConfig | None:
        """Return the configured storage location, or None if it is not in config."""
        cfg = self.projects.get(project)
        if cfg is None:
            return None
        try:
            return cfg.storage_location(name)
        except KeyError:
            return None

    @contextmanager
    def write_session(self) -> Iterator[Session]:
        """Open a write transaction (``BEGIN IMMEDIATE`` on SQLite); commits on success.

        Every write path uses this; plain ``sessionmaker()`` sessions are for reads.
        """
        with write_session(self.sessionmaker) as session:
            yield session

    def sync_config(self) -> None:
        """Upsert the loaded projects and storage locations into the DB (DB-03)."""
        with self.write_session() as session:
            lock_schema(session.connection())
            sync_config(session, self.projects)

    def dispose(self) -> None:
        """Close the engine's pooled connections."""
        self.engine.dispose()


_lock = threading.Lock()
_context: AppContext | None = None


def get_context() -> AppContext:
    """Return the process context, building it from settings on first use.

    Building it (as a flow-run process does) also installs the Prefect block
    resolver and the rclone hooks, unless they are installed, so storage
    locations that use Prefect blocks or rclone work there too (STO-18,
    STO-20). Blocks are looked up lazily, on use.
    """
    global _context
    with _lock:
        if _context is None:
            from heliopuddle.server.services.blocks import install_block_resolver
            from heliopuddle.server.services.rclone_hooks import install_rclone

            _context = AppContext.from_settings()
            install_block_resolver(replace=False)
            install_rclone(_context.settings, replace=False)
        return _context


def set_context(ctx: AppContext | None) -> None:
    """Install ``ctx`` as the process context (None resets it; for tests)."""
    global _context
    with _lock:
        _context = ctx


def reset_context() -> None:
    """Forget the process context so the next :func:`get_context` rebuilds it."""
    set_context(None)

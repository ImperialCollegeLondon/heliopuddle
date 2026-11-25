"""Helpers shared by the ``/projects`` endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from heliopuddle.config import ProjectConfig, StorageLocationConfig
from heliopuddle.server.schemas import ProjectRead
from heliopuddle.storage.backends import server_only
from heliopuddle.storage.credentials import location_problems

if TYPE_CHECKING:
    from heliopuddle.server.database.orm import IndexProgress, StorageLocation


def index_times(progress: IndexProgress | None) -> dict[str, datetime | None]:
    """The last full and incremental index times of a storage location."""
    return {
        "last_full_index_at": progress.last_full_at if progress else None,
        "last_incremental_index_at": progress.last_incremental_at if progress else None,
    }


def storage_location_read(
    row: StorageLocation,
    progress: IndexProgress | None,
    cfg: StorageLocationConfig | None,
    *,
    include_url: bool = True,
) -> dict[str, Any]:
    """The fields of a storage location's API record (``url`` blank unless ``include_url``).

    ``status`` and ``problems`` say whether its Prefect blocks resolve (STO-18);
    an inactive storage location (no ``cfg``) is ``ok``.
    """
    problems = location_problems(cfg) if cfg else []
    return {
        "name": row.name,
        "url": row.url if include_url else "",
        "accept_external_changes": row.accept_external_changes,
        "active": row.active,
        "create_root": cfg.create_root if cfg else "auto",
        "quota": cfg.quota if cfg else None,
        "status": "incomplete" if problems else "ok",
        "direct_access": not server_only(row.url),
        "problems": problems,
        **index_times(progress),
    }


def project_read(cfg: ProjectConfig) -> ProjectRead:
    """A project and the names of its storage locations."""
    return ProjectRead(name=cfg.project, storage_locations=[s.name for s in cfg.storage_locations])

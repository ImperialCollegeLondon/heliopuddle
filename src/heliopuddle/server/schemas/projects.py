"""Project and storage location schemas.

This module must not import SQLAlchemy or anything from ``heliopuddle.server``
other than the schema modules: the client imports it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ProjectRead(BaseModel):
    """A project and the names of its storage locations."""

    name: str
    storage_locations: list[str]


class StorageLocationRead(BaseModel):
    """A storage location as listed by ``GET /projects/{p}/storage-locations``."""

    name: str
    url: str = Field(
        description="Backend URL; empty without hp-client-storage-read-write on this project"
    )
    accept_external_changes: bool
    active: bool
    create_root: bool | Literal["auto"] = Field(
        default="auto", description="As configured; clients create a missing root the same way"
    )
    last_full_index_at: datetime | None = None
    last_incremental_index_at: datetime | None = None
    quota: int | None = None


class StorageLocationDetail(StorageLocationRead):
    """Backend details requiring hp-client-storage-read-write on the project or hp-admin."""

    storage_options: dict[str, Any] = Field(default_factory=dict)

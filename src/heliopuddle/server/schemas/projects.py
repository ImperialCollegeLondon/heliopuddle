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
    status: Literal["ok", "incomplete"] = Field(
        default="ok",
        description="incomplete: a Prefect block it needs is missing or cannot be loaded "
        "(STO-18); calls that need it return 409 storage_location_incomplete",
    )
    direct_access: bool = Field(
        default=True,
        description="False for storage locations only the server reaches (rclone, STO-05): "
        "clients transfer through the API",
    )
    problems: list[str] = Field(
        default_factory=list, description="Why the storage location is incomplete"
    )


class StorageLocationDetail(StorageLocationRead):
    """Backend details requiring hp-client-storage-read-write on the project or hp-admin.

    ``url`` and ``storage_options`` are resolved from Prefect blocks (STO-18,
    STO-19); while the storage location is incomplete they are the configured,
    unresolved values.
    """

    storage_options: dict[str, Any] = Field(default_factory=dict)


class StorageLocationIncompleteError(BaseModel):
    """Body of a ``409 storage_location_incomplete`` (STO-18)."""

    detail: str
    reason: Literal["storage_location_incomplete"] = "storage_location_incomplete"
    problems: list[str] = Field(default_factory=list)

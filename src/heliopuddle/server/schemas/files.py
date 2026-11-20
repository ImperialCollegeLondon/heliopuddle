"""File schemas: register, check, query, accept and series versions.

This module must not import SQLAlchemy or anything from ``heliopuddle.server``
other than the schema modules: the client imports it.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

MAX_VERSION_NUMBER = 2**63 - 1
"""The largest ``version_number`` (a signed 64-bit integer)."""
MAX_VERSION_TEXT = 255
"""The longest ``version_text``."""

VersionType = Literal["number", "text"]


class VersionOverride(BaseModel):
    """An optional version for a new file (API-20): a number or a text, not both."""

    version_number: int | None = Field(
        default=None,
        ge=0,
        le=MAX_VERSION_NUMBER,
        description="Number version of a new file; replaces the one from the version rules",
    )
    version_text: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_VERSION_TEXT,
        description="Text version of a new file; replaces the one from the version rules",
    )

    @model_validator(mode="after")
    def _one_version(self) -> VersionOverride:
        if self.version_number is not None and self.version_text is not None:
            raise ValueError("give version_number or version_text, not both")
        return self


class FileState(StrEnum):
    """State of one copy of a file in one storage location."""

    PENDING = "pending"
    AVAILABLE = "available"
    OVERWRITING = "overwriting"
    MISSING = "missing"
    DELETING = "deleting"
    DELETED = "deleted"
    UNKNOWN = "unknown"


class FileCreate(VersionOverride):
    """``POST /projects/{p}/files``: register a file already written to the store."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, description="Relative, '/'-separated path in the store")
    storage_location: str | None = Field(
        default=None, description="Storage location name; defaults to the project's first"
    )
    hash: str = Field(min_length=1, description="Hex digest of the content")
    size: int = Field(ge=0)
    metadata: dict[str, Any] | None = None
    content_date: datetime | None = Field(
        default=None, description="Overrides a date taken from the version rule's 'date' group"
    )
    overwrite: bool = Field(
        default=False, description="Replace a live file at this path that has a different hash"
    )


class ContentDateUpdate(BaseModel):
    """``PATCH /files/{id}/content-date``: set or clear a file's content date (API-19)."""

    model_config = ConfigDict(extra="forbid")

    content_date: datetime | None = Field(
        description="The new content date (a naive time is UTC); null goes back to the "
        "date in the path (a version rule's 'date' group), or none"
    )


class FileCheck(BaseModel):
    """``POST /projects/{p}/files/check``: does ``path`` exist with this hash?"""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    hash: str = Field(min_length=1)


class FileStorageLocationRead(BaseModel):
    """One copy of a file in one storage location."""

    storage_location: str
    state: FileState
    size: int | None = None
    mtime: datetime | None = None
    hash: str | None = None
    last_seen_at: datetime | None = None
    arrived_at: datetime | None = Field(
        default=None,
        description="When the file arrived in this storage location: first recorded there, "
        "or last copied or moved there by a rule (absent from older servers)",
    )
    updated_at: datetime


class FileRead(BaseModel):
    """A logical file and its copies. The client's ``FileRecord``."""

    id: int
    project: str
    path: str
    name: str
    series_id: int
    version_number: int | None = None
    version_text: str | None = None
    content_date: datetime | None = None
    size: int
    hash: str
    hash_algo: str
    metadata: dict[str, Any] | None = None
    is_latest: bool
    ignored_from_all_rules: bool = False
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None
    created_by: str | None = None
    updated_by: str | None = None
    series_key: str | None = None
    storage_locations: list[FileStorageLocationRead] = Field(
        default_factory=list, description="Copies, ordered by storage location"
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def state(self) -> FileState | None:
        """State of the first copy (the only copy in M1); None if there are no copies."""
        return self.storage_locations[0].state if self.storage_locations else None


class FileList(BaseModel):
    """A page of files."""

    items: list[FileRead]
    total: int
    limit: int
    offset: int


class FileCheckResult(BaseModel):
    """Answer to ``POST /projects/{p}/files/check``."""

    exists: bool
    hash_matches: bool
    file: FileRead | None = None


class FileConflict(BaseModel):
    """Body of a ``409``: the live file at the path conflicts with the request.

    It has a different hash, or a version that differs from the request's
    version override (API-22); ``detail`` says which.
    """

    detail: str
    file: FileRead


class Busy(BaseModel):
    """Body of a ``409 busy``: another operation owns the path (sent with ``Retry-After``).

    See docs/design/operations.md ("Ownership").
    """

    detail: str
    reason: Literal["busy"] = "busy"
    copy_id: int | None = None
    deadline: datetime | None = Field(
        default=None, description="When the owner's reservation expires, if known"
    )


class SeriesVersions(BaseModel):
    """``GET /series/{id}/versions``: versions newest first."""

    series_id: int
    series_key: str
    project: str
    version_type: VersionType | None = Field(
        default=None, description="'number' or 'text'; null while the series has no version"
    )
    items: list[FileRead]


class AcceptRequest(BaseModel):
    """``POST /files/{id}/accept``: accept an ``unknown`` copy as it is now."""

    model_config = ConfigDict(extra="forbid")

    storage_location: str | None = Field(
        default=None, description="Defaults to the file's only unknown copy"
    )

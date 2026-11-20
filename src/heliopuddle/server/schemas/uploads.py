"""Upload reservation schemas.

This module must not import SQLAlchemy or anything from ``heliopuddle.server``
other than the schema modules: the client imports it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from heliopuddle.server.schemas.files import FileRead, VersionOverride

UploadMode = Literal["new", "overwrite", "unchanged"]


class UploadRequest(VersionOverride):
    """``POST /projects/{p}/uploads``: reserve a path before writing it.

    See docs/design/operations.md ("Add a file", "Overwrite").
    """

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    hash: str = Field(min_length=1, description="Hex md5 of the content to write")
    size: int = Field(ge=0)
    storage_location: str | None = None
    overwrite: bool = False
    metadata: dict[str, Any] | None = None
    content_date: datetime | None = None


class UploadReservation(BaseModel):
    """Answer to a reservation: where to write, and the token that owns the path.

    For ``mode == "unchanged"`` nothing is written: ``token`` and
    ``write_path`` are None and ``file`` is the existing record.
    """

    mode: UploadMode
    token: str | None = None
    write_path: str | None = Field(
        default=None, description="Relative path to write: the final path, or a staging path"
    )
    deadline: datetime | None = None
    file: FileRead | None = None

"""``GET /projects/{project}/files/by-path``: the live file at a path."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from heliopuddle.server import models
from heliopuddle.server.api.deps import (
    Ctx,
    require_project,
    requires,
)
from heliopuddle.server.api.files._shared import validate_path
from heliopuddle.server.auth.roles import ROLE_READER
from heliopuddle.server.schemas import (
    FileRead,
)

router = APIRouter()


@router.get(
    "/projects/{project}/files/by-path",
    response_model=FileRead,
    dependencies=[Depends(requires(ROLE_READER))],
)
def get_file_by_path(project: str, path: Annotated[str, Query(min_length=1)], ctx: Ctx) -> FileRead:
    """Return the live file at ``path``, or ``404``."""
    require_project(ctx, project)
    validate_path(path, loc="query")
    with ctx.sessionmaker() as session:
        file = models.get_file_by_path(session, project, path)
        if file is None:
            raise HTTPException(status_code=404, detail=f"no live file at {path!r}")
        return models.to_file_read(file)

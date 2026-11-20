"""``PATCH /files/{file_id}/metadata``: merge or replace a file's metadata (API-08)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException

from heliopuddle.server import models
from heliopuddle.server.api.deps import (
    Ctx,
    User,
    file_project,
    requires,
)
from heliopuddle.server.api.files._shared import read_file
from heliopuddle.server.auth.roles import ROLE_WRITER
from heliopuddle.server.schemas import (
    FileRead,
)
from heliopuddle.server.services.rule_control import notify_file_changes

router = APIRouter()


@router.patch(
    "/files/{file_id}/metadata",
    response_model=FileRead,
    dependencies=[Depends(requires(ROLE_WRITER, project_of=file_project))],
)
def patch_metadata(
    file_id: int,
    ctx: Ctx,
    user: User,
    body: Annotated[dict[str, Any], Body(description="Keys to set; null removes a key")],
    replace: bool = False,
) -> FileRead:
    """Merge ``body`` into a live file's metadata (``null`` removes a key), or replace it."""
    try:
        with ctx.write_session() as session:
            models.update_metadata(session, file_id, body, replace=replace, updated_by=user)
    except models.NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None
    file = read_file(ctx, file_id)
    notify_file_changes(ctx, file.project, [file_id])
    return file

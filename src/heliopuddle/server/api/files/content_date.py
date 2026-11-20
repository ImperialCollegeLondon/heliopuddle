"""``PATCH /files/{file_id}/content-date``: set or clear a file's content date (API-19)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from heliopuddle.server import models
from heliopuddle.server.api.deps import (
    Ctx,
    User,
    file_project,
    require_project,
    requires,
)
from heliopuddle.server.api.files._shared import read_file
from heliopuddle.server.auth.roles import ROLE_WRITER
from heliopuddle.server.schemas import ContentDateUpdate, FileRead
from heliopuddle.server.services.rule_control import notify_file_changes

router = APIRouter()


@router.patch(
    "/files/{file_id}/content-date",
    response_model=FileRead,
    dependencies=[Depends(requires(ROLE_WRITER, project_of=file_project))],
)
def patch_content_date(file_id: int, body: ContentDateUpdate, ctx: Ctx, user: User) -> FileRead:
    """Set a live file's content date; null goes back to the date in its path, or none."""
    project = read_file(ctx, file_id).project
    rules = require_project(ctx, project).versioning
    try:
        with ctx.write_session() as session:
            models.set_content_date(
                session, file_id, body.content_date, rules=rules, updated_by=user
            )
    except models.NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None
    file = read_file(ctx, file_id)
    notify_file_changes(ctx, file.project, [file_id])
    return file

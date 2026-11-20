"""``PATCH /files/{file_id}/rules-ignore``: exclude a file from every rule."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

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
    RulesIgnoreRequest,
)
from heliopuddle.server.services.rule_control import notify_file_changes

router = APIRouter()


@router.patch(
    "/files/{file_id}/rules-ignore",
    response_model=FileRead,
    dependencies=[Depends(requires(ROLE_WRITER, project_of=file_project))],
)
def set_rules_ignored(file_id: int, body: RulesIgnoreRequest, ctx: Ctx, user: User) -> FileRead:
    """Set a file's exclusion from every rule; indexing is unaffected."""
    try:
        with ctx.write_session() as session:
            models.set_rules_ignored(session, file_id, body.ignored, updated_by=user)
    except models.NotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from None
    file = read_file(ctx, file_id)
    if not body.ignored:
        notify_file_changes(ctx, file.project, [file_id])
    return file

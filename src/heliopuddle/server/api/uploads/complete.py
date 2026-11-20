"""``POST /uploads/{token}/complete``: verify or publish, return the record (STO-10)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from heliopuddle.server.api.deps import Ctx, User, requires, upload_project
from heliopuddle.server.api.errors import conflict
from heliopuddle.server.api.uploads._shared import not_current, read_file
from heliopuddle.server.auth.roles import ROLE_WRITER
from heliopuddle.server.models import uploads as uploads_db
from heliopuddle.server.schemas import (
    FileConflict,
    FileRead,
)
from heliopuddle.server.services.rule_control import notify_file_changes
from heliopuddle.server.services.upload import (
    ObjectUnobservable,
    PublishFailed,
    complete_upload,
)

router = APIRouter()


@router.post(
    "/uploads/{token}/complete",
    dependencies=[Depends(requires(ROLE_WRITER, project_of=upload_project))],
    response_model=FileRead,
    responses={
        409: {
            "model": FileConflict,
            "description": "The token is not current, or the overwrite did not publish",
        },
        503: {"description": "The object could not be read; the upload is kept, retry complete"},
    },
)
def complete(token: str, ctx: Ctx, user: User) -> FileRead | JSONResponse:
    """Complete an upload: verify a new file, or publish an overwrite; return the record.

    A new file's copy ends ``available`` (hash and size match) or ``unknown``.
    An overwrite that cannot publish keeps the original and returns ``409``
    with the record.
    """
    try:
        result = complete_upload(token, user)
    except uploads_db.TokenNotCurrent as e:
        return not_current(e)
    except PublishFailed as e:
        try:
            return conflict(str(e), read_file(ctx, e.file_id))
        except HTTPException:  # the reservation created the file and was dropped
            return not_current(e)
    except ObjectUnobservable as e:
        return JSONResponse(
            status_code=503, content={"detail": str(e)}, headers={"Retry-After": "1"}
        )
    file = read_file(ctx, result.file_id)
    notify_file_changes(ctx, file.project, [result.file_id])
    return file

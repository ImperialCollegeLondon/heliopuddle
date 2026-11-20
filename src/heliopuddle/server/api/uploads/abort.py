"""``DELETE /uploads/{token}``: abort an upload and release the path."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse

from heliopuddle.server.api.deps import requires, upload_project
from heliopuddle.server.api.uploads._shared import not_current
from heliopuddle.server.auth.roles import ROLE_WRITER
from heliopuddle.server.models import uploads as uploads_db
from heliopuddle.server.services.upload import (
    RepairResult,
    abort_upload,
)

router = APIRouter()


@router.delete(
    "/uploads/{token}",
    dependencies=[Depends(requires(ROLE_WRITER, project_of=upload_project))],
    status_code=204,
    responses={
        409: {"description": "The token is not current"},
        503: {"description": "The abort did not finish; the reservation will be repaired"},
    },
)
def abort(token: str) -> Response:
    """Abort an upload: remove what it wrote and release the path."""
    try:
        result = abort_upload(token)
    except uploads_db.TokenNotCurrent as e:
        return not_current(e)
    if result == RepairResult.INCOMPLETE:
        return JSONResponse(
            status_code=503,
            content={"detail": "the abort did not finish; the reservation will be repaired"},
        )
    return Response(status_code=204)

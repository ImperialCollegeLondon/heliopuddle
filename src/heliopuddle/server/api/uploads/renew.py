"""``POST /uploads/{token}/renew``: extend a reservation's deadline."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from heliopuddle.server.api.deps import Ctx, requires, upload_project
from heliopuddle.server.api.uploads._shared import not_current
from heliopuddle.server.auth.roles import ROLE_WRITER
from heliopuddle.server.models import uploads as uploads_db
from heliopuddle.server.schemas import (
    UploadReservation,
)

router = APIRouter()


@router.post(
    "/uploads/{token}/renew",
    dependencies=[Depends(requires(ROLE_WRITER, project_of=upload_project))],
    response_model=UploadReservation,
    responses={409: {"description": "The token is not current"}},
)
def renew_upload(token: str, ctx: Ctx) -> UploadReservation | JSONResponse:
    """Extend a reservation's deadline by the lease (``HELIOPUDDLE_OP_LEASE``)."""
    try:
        with ctx.write_session() as session:
            res = uploads_db.renew(session, token, ctx.settings.op_lease)
    except uploads_db.TokenNotCurrent as e:
        return not_current(e)
    return UploadReservation(
        mode=res.mode, token=res.token, write_path=res.write_path, deadline=res.deadline
    )

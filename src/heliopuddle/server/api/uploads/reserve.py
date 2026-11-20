"""``POST /projects/{project}/uploads``: reserve a path before writing it (STO-09)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from heliopuddle.server import models
from heliopuddle.server.api.deps import Ctx, User, require_project, requires
from heliopuddle.server.api.errors import (
    check_upload_size,
)
from heliopuddle.server.api.repair import attempt_write
from heliopuddle.server.api.uploads._shared import read_file
from heliopuddle.server.auth.roles import ROLE_WRITER
from heliopuddle.server.models import uploads as uploads_db
from heliopuddle.server.schemas import (
    Busy,
    FileConflict,
    UploadRequest,
    UploadReservation,
)
from heliopuddle.server.services.upload import (
    object_present,
)

router = APIRouter()


@router.post(
    "/projects/{project}/uploads",
    dependencies=[Depends(requires(ROLE_WRITER))],
    response_model=UploadReservation,
    responses={
        409: {
            "model": FileConflict | Busy,
            "description": "A different file exists at the path, a version override "
            "differs from its version, or the path is busy",
        },
        404: {"description": "Unknown project or storage location"},
        413: {"description": "The declared size exceeds max_upload_size"},
        422: {
            "description": "Invalid path; both version overrides; an invalid version from "
            "a rule; or a version of the other kind than the series"
        },
    },
)
def reserve_upload(
    project: str, body: UploadRequest, ctx: Ctx, user: User
) -> UploadReservation | JSONResponse:
    """Reserve a path before writing it; the answer says what and where to write.

    ``mode`` is ``unchanged`` (same hash, copy ``available``: write nothing),
    ``new`` (write ``write_path``: the final path, or a staging path when an
    object already exists there) or ``overwrite`` (write
    ``write_path``, a staging path; requires ``overwrite`` and an
    ``available`` copy). A copy holding an expired operation is repaired
    first.
    """
    cfg = require_project(ctx, project)
    check_upload_size(ctx.settings, body.size)
    location = body.storage_location or cfg.storage_locations[0].name
    try:
        models.validate_path(body.path)
    except ValueError as e:
        raise HTTPException(
            status_code=422,
            detail=[{"loc": ["body", "path"], "msg": str(e), "type": "value_error"}],
        ) from None

    def attempt() -> uploads_db.Reservation:
        # Looked at outside the transaction: a small race remains (an object that
        # appears after this check is written over by a direct write).
        final_exists = object_present(ctx, project, location, body.path)
        with ctx.write_session() as session:
            return uploads_db.reserve(
                session,
                project=project,
                storage_location=location,
                path=body.path,
                hash=body.hash,
                size=body.size,
                lease=ctx.settings.op_lease,
                metadata=body.metadata,
                created_by=user,
                rules=cfg.versioning,
                content_date=body.content_date,
                overwrite=body.overwrite,
                final_exists=final_exists,
                version_number=body.version_number,
                version_text=body.version_text,
            )

    res = attempt_write(ctx, attempt, read_file, also_conflicts=(uploads_db.NotOverwritable,))
    if isinstance(res, JSONResponse):
        return res
    return UploadReservation(
        mode=res.mode,
        token=res.token,
        write_path=res.write_path,
        deadline=res.deadline,
        file=read_file(ctx, res.file_id),
    )

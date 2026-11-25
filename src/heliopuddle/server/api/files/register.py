"""``POST /projects/{project}/files``: register a file already in the store (API-01)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse

from heliopuddle.server import models
from heliopuddle.server.api.deps import (
    Ctx,
    User,
    require_project,
    requires,
)
from heliopuddle.server.api.errors import (
    check_upload_size,
    require_covered,
)
from heliopuddle.server.api.files._shared import read_file, validate_path
from heliopuddle.server.api.repair import attempt_write
from heliopuddle.server.auth.roles import ROLE_WRITER
from heliopuddle.server.schemas import (
    Busy,
    FileConflict,
    FileCreate,
    FileRead,
)
from heliopuddle.server.services.rule_control import notify_file_changes
from heliopuddle.server.services.verify import verify_file

router = APIRouter()

logger = logging.getLogger(__name__)


@router.post(
    "/projects/{project}/files",
    dependencies=[Depends(requires(ROLE_WRITER))],
    status_code=201,
    response_model=FileRead,
    responses={
        200: {"model": FileRead, "description": "Same hash: the existing file"},
        409: {
            "model": FileConflict | Busy,
            "description": "A different file exists at this path (overwrites use "
            "/projects/{p}/uploads), a version override differs from its version, "
            "or the path is busy",
        },
        413: {"description": "The declared size exceeds max_upload_size"},
        422: {
            "description": "Invalid path; both version overrides; an invalid version from "
            "a rule; or a version of the other kind than the series"
        },
    },
)
def register_file(
    project: str, body: FileCreate, ctx: Ctx, user: User, response: Response
) -> FileRead | JSONResponse:
    """Register a file already written to the store, then verify it inline.

    The upload protocol's reserve and complete in one call, for a file another
    tool has written: a new path returns ``201``; at a live path, the same
    hash returns ``200`` with the existing file (re-verified only if its copy
    is not ``available``); a different hash returns ``409`` with the existing
    file. A version override applies to a new file only; at a live path it
    must equal the file's version, else ``409`` (API-22). ``overwrite`` is not
    done here: a different hash with ``overwrite`` also returns ``409``,
    pointing at ``POST /projects/{p}/uploads``. A path owned by another
    operation returns ``409 busy`` with ``Retry-After``.
    The registration is committed before ``verify_file`` runs; if the task
    function raises, the copy stays ``pending``.
    """
    cfg = require_project(ctx, project)
    check_upload_size(ctx.settings, body.size)
    location = body.storage_location or cfg.storage_locations[0].name
    validate_path(body.path)
    require_covered(ctx, project, location, body.path)

    def conflict_detail(e: models.ConflictError) -> str:
        detail = str(e)
        if body.overwrite:
            detail += f"; to overwrite it, use POST /projects/{project}/uploads"
        return detail

    def attempt() -> models.RegisterResult:
        with ctx.write_session() as session:
            return models.register_file(
                session,
                project=project,
                storage_location=location,
                path=body.path,
                hash=body.hash,
                size=body.size,
                metadata=body.metadata,
                created_by=user,
                rules=cfg.versioning,
                content_date=body.content_date,
                version_number=body.version_number,
                version_text=body.version_text,
            )

    result = attempt_write(ctx, attempt, read_file, conflict_detail=conflict_detail)
    if isinstance(result, JSONResponse):
        return result

    if result.needs_verify:
        try:
            verify_file.fn(result.copy_id)
        except Exception:
            logger.exception("verify_file(%s) failed; the copy stays pending", result.copy_id)

    if result.outcome != models.RegisterOutcome.CREATED:
        response.status_code = 200
    file = read_file(ctx, result.file_id)
    if result.outcome == models.RegisterOutcome.CREATED or result.needs_verify:
        notify_file_changes(ctx, project, [result.file_id])
    return file

"""``DELETE /files/{file_id}``: remove a file (API-07)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from heliopuddle.server import models
from heliopuddle.server.api.deps import (
    Ctx,
    User,
    file_project,
    requires,
)
from heliopuddle.server.api.errors import busy
from heliopuddle.server.api.files._shared import read_file
from heliopuddle.server.auth.roles import ROLE_DELETER
from heliopuddle.server.schemas import (
    Busy,
    FileRead,
)
from heliopuddle.server.services.delete import delete_file

router = APIRouter()

logger = logging.getLogger(__name__)


@router.delete(
    "/files/{file_id}",
    dependencies=[Depends(requires(ROLE_DELETER, project_of=file_project))],
    response_model=FileRead,
    responses={
        409: {"model": Busy, "description": "Another operation owns a copy (Retry-After)"},
        500: {"description": "The delete failed unexpectedly"},
    },
)
def remove_file(file_id: int, ctx: Ctx, user: User) -> FileRead | JSONResponse:
    """Remove a file by calling the ``delete_file`` task's function inline; return the final record.

    ``200`` with the record: the file is deleted, or (an expected incomplete
    delete) a copy whose store delete failed stays ``deleting`` and is
    retried by the index, or a copy that did not match hp's last observation
    was left in the store as ``unknown``. A deleted or unknown file returns
    ``404``; a copy owned by another operation ``409 busy``; an unexpected
    error ``500``.
    """
    current = read_file(ctx, file_id)
    if current.deleted_at is not None:
        raise HTTPException(status_code=404, detail=f"file {file_id} not found")
    try:
        delete_file.fn(file_id, user)
    except models.NotFoundError:
        raise HTTPException(status_code=404, detail=f"file {file_id} not found") from None
    except models.CopyBusy as e:
        return busy(e.copy_id, e.deadline)
    except Exception as e:
        logger.error("delete_file(%s) failed (%s)", file_id, type(e).__name__)
        raise HTTPException(
            status_code=500, detail=f"deleting file {file_id} failed: {type(e).__name__}"
        ) from e
    return read_file(ctx, file_id)

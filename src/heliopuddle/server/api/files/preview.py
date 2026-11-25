"""``GET /files/{file_id}/preview``: a bounded preview of a live file (API-18)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from heliopuddle.server.api.deps import (
    Ctx,
    file_project,
    requires,
)
from heliopuddle.server.auth.roles import ROLE_READER
from heliopuddle.server.schemas import (
    FilePreview,
)
from heliopuddle.server.services.preview import preview_file
from heliopuddle.storage.rclone import RcloneBusyError

router = APIRouter()

logger = logging.getLogger(__name__)


@router.get(
    "/files/{file_id}/preview",
    response_model=FilePreview,
    dependencies=[Depends(requires(ROLE_READER, project_of=file_project))],
    responses={
        404: {"description": "The file is deleted or has no readable copy"},
        503: {"description": "The backing storage could not be read"},
    },
)
def file_preview(file_id: int, ctx: Ctx) -> FilePreview:
    """Return a bounded preview of a live file's content, read on the server (API-18).

    Content that cannot be parsed as its format gives kind ``error``, never ``500``.
    """
    try:
        return preview_file.fn(file_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="file content not found") from None
    except RcloneBusyError:
        raise  # the global handler answers 503 with Retry-After
    except OSError as error:
        logger.error("API file preview read failed (%s)", type(error).__name__)
        raise HTTPException(status_code=503, detail="backing storage read failed") from None

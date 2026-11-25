"""``GET /files/{file_id}/content``: stream a readable copy."""

from __future__ import annotations

import logging
from collections.abc import Iterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from heliopuddle.server.api.deps import (
    Ctx,
    file_project,
    requires,
)
from heliopuddle.server.auth.roles import ROLE_READER
from heliopuddle.server.services.content import open_file_content
from heliopuddle.storage.hashing import CHUNK_SIZE
from heliopuddle.storage.rclone import RcloneBusyError

router = APIRouter()

logger = logging.getLogger(__name__)


@router.get(
    "/files/{file_id}/content",
    dependencies=[Depends(requires(ROLE_READER, project_of=file_project))],
    response_class=StreamingResponse,
    responses={
        404: {"description": "No live, readable copy exists"},
        503: {"description": "The backing storage could not be read"},
    },
)
def file_content(file_id: int, ctx: Ctx) -> StreamingResponse:
    """Stream a configured, readable copy without issuing backend credentials."""
    try:
        stream, first = open_file_content.fn(file_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="file content not found") from None
    except RcloneBusyError:
        raise  # the global handler answers 503 with Retry-After
    except OSError as error:
        logger.error("API file content read failed (%s)", type(error).__name__)
        raise HTTPException(status_code=503, detail="backing storage read failed") from None

    def chunks() -> Iterator[bytes]:
        try:
            yield first
            while chunk := stream.read(CHUNK_SIZE):
                yield chunk
        except Exception as error:
            logger.error("API file content stream failed (%s)", type(error).__name__)
            raise OSError("backing storage read failed") from None
        finally:
            stream.close()

    return StreamingResponse(
        chunks(),
        media_type="application/octet-stream",
        headers={"Cache-Control": "no-store"},
        background=BackgroundTask(stream.close),
    )

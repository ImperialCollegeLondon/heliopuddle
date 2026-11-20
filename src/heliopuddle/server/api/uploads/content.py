"""``PUT /uploads/{token}/content``: receive bounded, hash-checked content."""

from __future__ import annotations

import hashlib
import logging
import tempfile
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from heliopuddle.server.api.deps import Ctx, requires, upload_project
from heliopuddle.server.api.errors import busy
from heliopuddle.server.api.uploads._shared import not_current
from heliopuddle.server.auth.roles import ROLE_WRITER
from heliopuddle.server.models import transitions as tr
from heliopuddle.server.models import uploads as uploads_db
from heliopuddle.server.services.upload import (
    write_upload_content,
)

router = APIRouter()

logger = logging.getLogger(__name__)


@router.put(
    "/uploads/{token}/content",
    dependencies=[Depends(requires(ROLE_WRITER, project_of=upload_project))],
    status_code=204,
    responses={
        409: {"description": "The upload is no longer current"},
        413: {"description": "Content exceeds the reserved size"},
        422: {"description": "Content does not match the reserved hash and size"},
        503: {"description": "Content could not be written to the backing store"},
    },
)
async def upload_content(token: str, request: Request, ctx: Ctx) -> Response:
    """Receive bounded, hash-checked binary content without issuing store credentials."""

    def expectations() -> tuple[str, int]:
        with ctx.write_session() as session:
            return uploads_db.content_expectations(session, token, ctx.settings.op_lease)

    try:
        expected_hash, expected_size = await run_in_threadpool(expectations)
        renewed = time.monotonic()
        interval = max(ctx.settings.op_lease.total_seconds() / 3, 0.1)
        digest = hashlib.md5(usedforsecurity=False)
        received = 0
        with tempfile.TemporaryFile() as spool:
            async for chunk in request.stream():
                received += len(chunk)
                if received > expected_size:
                    raise HTTPException(status_code=413, detail="content exceeds reserved size")
                digest.update(chunk)
                await run_in_threadpool(spool.write, chunk)
                if time.monotonic() - renewed >= interval:
                    await run_in_threadpool(expectations)
                    renewed = time.monotonic()
            if received != expected_size or digest.hexdigest() != expected_hash.lower():
                raise HTTPException(
                    status_code=422, detail="content does not match reserved hash and size"
                )
            spool.seek(0)
            await run_in_threadpool(write_upload_content.fn, token, spool)
    except (uploads_db.TokenNotCurrent, tr.StaleOperation) as error:
        return not_current(error)
    except tr.CopyBusy as error:
        return busy(error.copy_id, error.deadline)
    except HTTPException:
        raise
    except OSError as error:
        logger.error("API upload write failed (%s)", type(error).__name__)
        return JSONResponse(
            status_code=503,
            content={"detail": "backing storage write failed"},
            headers={"Retry-After": "1"},
        )
    return Response(status_code=204)

"""``POST /files/{file_id}/accept``: accept an ``unknown`` copy as it is now."""

from __future__ import annotations

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
from heliopuddle.server.auth.roles import ROLE_WRITER
from heliopuddle.server.schemas import (
    AcceptRequest,
    FileRead,
)
from heliopuddle.server.services.accept import (
    AmbiguousCopyError,
    NotUnknownError,
    accept_copy,
)
from heliopuddle.server.services.rule_control import notify_file_changes

router = APIRouter()


@router.post(
    "/files/{file_id}/accept",
    dependencies=[Depends(requires(ROLE_WRITER, project_of=file_project))],
    response_model=FileRead,
    responses={
        404: {"description": "Unknown or deleted file, or unknown storage location"},
        409: {"description": "The copy is not unknown, or its object is not in the store"},
        422: {"description": "No storage location given and not exactly one unknown copy"},
    },
)
def accept_file(
    file_id: int, ctx: Ctx, user: User, body: AcceptRequest | None = None
) -> FileRead | JSONResponse:
    """Accept an ``unknown`` copy as it is now by running ``accept_copy`` inline.

    The copy is re-hashed; the file takes its hash and size, the copy becomes
    ``available`` and ``is_latest`` is recomputed. ``storage_location``
    defaults to the file's only ``unknown`` copy.
    """
    location = body.storage_location if body is not None else None
    try:
        accept_copy.fn(file_id, location, user)
    except models.NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None
    except NotUnknownError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    except models.CopyBusy as e:
        return busy(e.copy_id, e.deadline)
    except AmbiguousCopyError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    except FileNotFoundError:
        raise HTTPException(
            status_code=409, detail=f"file {file_id}: the object is not in the store"
        ) from None
    except LookupError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    file = read_file(ctx, file_id)
    notify_file_changes(ctx, file.project, [file_id])
    return file

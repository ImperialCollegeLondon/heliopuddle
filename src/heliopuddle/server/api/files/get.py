"""``GET /files/{file_id}``: one file with the state of each copy."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from heliopuddle.server.api.deps import (
    Ctx,
    file_project,
    requires,
)
from heliopuddle.server.api.files._shared import read_file
from heliopuddle.server.auth.roles import ROLE_READER
from heliopuddle.server.schemas import (
    FileRead,
)

router = APIRouter()


@router.get(
    "/files/{file_id}",
    response_model=FileRead,
    dependencies=[Depends(requires(ROLE_READER, project_of=file_project))],
)
def get_file(file_id: int, ctx: Ctx) -> FileRead:
    """Return one file (live or deleted) with the state of each copy."""
    return read_file(ctx, file_id)

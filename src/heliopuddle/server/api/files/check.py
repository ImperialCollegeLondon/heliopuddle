"""``POST /projects/{project}/files/check``: does a path exist with this hash (API-02)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from heliopuddle.server import models
from heliopuddle.server.api.deps import (
    Ctx,
    require_project,
    requires,
)
from heliopuddle.server.api.files._shared import validate_path
from heliopuddle.server.auth.roles import ROLE_READER
from heliopuddle.server.schemas import (
    FileCheck,
    FileCheckResult,
)

router = APIRouter()


@router.post(
    "/projects/{project}/files/check",
    response_model=FileCheckResult,
    dependencies=[Depends(requires(ROLE_READER))],
)
def check_file(project: str, body: FileCheck, ctx: Ctx) -> FileCheckResult:
    """Is there a live file at ``path``, and does it have this hash?"""
    require_project(ctx, project)
    validate_path(body.path)
    with ctx.sessionmaker() as session:
        file = models.get_file_by_path(session, project, body.path)
        if file is None:
            return FileCheckResult(exists=False, hash_matches=False)
        return FileCheckResult(
            exists=True,
            hash_matches=file.hash == body.hash.lower(),
            file=models.to_file_read(file),
        )

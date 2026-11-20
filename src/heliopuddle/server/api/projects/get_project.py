"""``GET /projects/{project}``: one project (API-03)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from heliopuddle.server.api.deps import Ctx, require_project, requires
from heliopuddle.server.api.projects._shared import project_read
from heliopuddle.server.auth.roles import ROLE_READER
from heliopuddle.server.schemas import ProjectRead

router = APIRouter()


@router.get(
    "/projects/{project}", response_model=ProjectRead, dependencies=[Depends(requires(ROLE_READER))]
)
def get_project(project: str, ctx: Ctx) -> ProjectRead:
    """Return one project and the names of its (active) storage locations."""
    return project_read(require_project(ctx, project))

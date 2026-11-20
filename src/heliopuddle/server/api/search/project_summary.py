"""``GET /projects/{project}/summary``: file, copy and view counts (API-17)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from heliopuddle.server import models
from heliopuddle.server.api.deps import Ctx, require_project, requires
from heliopuddle.server.auth.roles import ROLE_READER
from heliopuddle.server.schemas import ProjectSummary

router = APIRouter()


@router.get(
    "/projects/{project}/summary",
    response_model=ProjectSummary,
    dependencies=[Depends(requires(ROLE_READER))],
)
def project_summary(project: str, ctx: Ctx) -> ProjectSummary:
    """Return a project's file, copy and view counts from aggregate queries."""
    cfg = require_project(ctx, project)
    with ctx.sessionmaker() as session:
        return models.project_summary(
            session, project, [location.name for location in cfg.storage_locations]
        )

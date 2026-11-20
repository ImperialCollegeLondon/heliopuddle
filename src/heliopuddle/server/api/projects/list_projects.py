"""``GET /projects``: the projects the caller holds a role on (AUTH-08)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from heliopuddle.server.api.deps import Ctx, CurrentPrincipal, requires
from heliopuddle.server.api.projects._shared import project_read
from heliopuddle.server.schemas import ProjectRead

router = APIRouter()


@router.get("/projects", response_model=list[ProjectRead], dependencies=[Depends(requires(None))])
def list_projects(ctx: Ctx, principal: CurrentPrincipal) -> list[ProjectRead]:
    """List the configured projects the caller holds any role on (AUTH-08)."""
    return [
        project_read(cfg) for name, cfg in ctx.projects.items() if principal.has_any_role_on(name)
    ]

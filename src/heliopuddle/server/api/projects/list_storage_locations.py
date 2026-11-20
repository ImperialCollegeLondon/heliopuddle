"""``GET /projects/{project}/storage-locations`` (API-05)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from heliopuddle.server import models
from heliopuddle.server.api.deps import Ctx, CurrentPrincipal, require_project, requires
from heliopuddle.server.api.projects._shared import storage_location_read
from heliopuddle.server.auth.roles import ROLE_CLIENT_STORAGE_READ_WRITE, ROLE_READER
from heliopuddle.server.schemas import StorageLocationRead

router = APIRouter()


@router.get(
    "/projects/{project}/storage-locations",
    response_model=list[StorageLocationRead],
    dependencies=[Depends(requires(ROLE_READER))],
)
def list_storage_locations(
    project: str, request: Request, ctx: Ctx, principal: CurrentPrincipal
) -> list[StorageLocationRead]:
    """List storage locations, introspecting credential-bearing URLs before issuance."""
    require_project(ctx, project)
    include_url = principal.can(ROLE_CLIENT_STORAGE_READ_WRITE, project)
    if include_url:
        requires(ROLE_CLIENT_STORAGE_READ_WRITE)(request, ctx, principal)
    with ctx.sessionmaker() as session:
        rows = models.list_storage_locations(session, project)
        progress = models.get_index_progress(session, [row.id for row in rows])
        return [
            StorageLocationRead(
                **storage_location_read(
                    row,
                    progress.get(row.id),
                    ctx.storage_location_config(project, row.name) if row.active else None,
                    include_url=include_url,
                )
            )
            for row in rows
        ]

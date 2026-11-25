"""``GET /projects/{project}/storage-locations/{name}``: backend details (API-10)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from heliopuddle.server import models
from heliopuddle.server.api.deps import Ctx, require_project, requires
from heliopuddle.server.api.projects._shared import storage_location_read
from heliopuddle.server.auth.roles import ROLE_CLIENT_STORAGE_READ_WRITE
from heliopuddle.server.schemas import StorageLocationDetail
from heliopuddle.storage.credentials import StorageLocationIncomplete, resolve

router = APIRouter()


@router.get(
    "/projects/{project}/storage-locations/{name}",
    response_model=StorageLocationDetail,
    dependencies=[Depends(requires(ROLE_CLIENT_STORAGE_READ_WRITE))],
)
def get_storage_location(project: str, name: str, ctx: Ctx) -> StorageLocationDetail:
    """Return backend credentials to callers with the project's credential role.

    The URL and storage options come from the loaded config (``${VAR}``
    already resolved) with Prefect blocks resolved (STO-18, STO-19); while a
    block is missing they are the configured values and ``status`` is
    ``incomplete``. An inactive storage location has no options. Arbitrary
    fsspec options and URLs can carry credentials, which must not bypass the
    API's role checks.
    """
    require_project(ctx, project)
    with ctx.sessionmaker() as session:
        row = models.get_storage_location(session, project, name)
        if row is None:
            raise HTTPException(
                status_code=404, detail=f"storage location {name!r} not found in {project!r}"
            )
        cfg = ctx.storage_location_config(project, name) if row.active else None
        progress = models.get_index_progress(session, [row.id]).get(row.id)
        record = storage_location_read(row, progress, cfg)
        options: dict[str, Any] = {}
        if cfg is not None:
            try:
                record["url"], options = resolve(cfg)
            except StorageLocationIncomplete:
                if isinstance(cfg.storage_options, dict):
                    options = dict(cfg.storage_options)
        return StorageLocationDetail(**record, storage_options=options)

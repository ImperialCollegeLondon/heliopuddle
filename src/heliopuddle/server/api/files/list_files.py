"""``GET /projects/{project}/files``: query a project's files (API-02)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from heliopuddle.server import models
from heliopuddle.server.api.deps import (
    Ctx,
    require_project,
    requires,
)
from heliopuddle.server.auth.roles import ROLE_READER
from heliopuddle.server.models.files import Order
from heliopuddle.server.schemas import (
    FileList,
    FileState,
)

router = APIRouter()


@router.get(
    "/projects/{project}/files",
    response_model=FileList,
    dependencies=[Depends(requires(ROLE_READER))],
)
def list_files(
    project: str,
    ctx: Ctx,
    path: Annotated[
        str | None, Query(description="Path glob: '*' (one segment), '**' (any depth), '?'")
    ] = None,
    series_id: int | None = None,
    latest_only: bool = False,
    state: Annotated[
        list[FileState] | None,
        Query(
            description="Files with a copy in this state, or in any of the states when "
            "repeated (in storage_location if given)"
        ),
    ] = None,
    storage_location: Annotated[
        str | None, Query(description="Files with a (non-deleted) copy in this storage location")
    ] = None,
    content_date_from: Annotated[datetime | None, Query(description="Inclusive")] = None,
    content_date_to: Annotated[datetime | None, Query(description="Inclusive")] = None,
    include_deleted: bool = False,
    deleted_only: Annotated[
        bool, Query(description="Only deleted files (implies include_deleted)")
    ] = False,
    ignored_from_all_rules: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    order: Annotated[Order, Query()] = "-created_at",
) -> FileList:
    """List a project's files matching every given filter (newest first by default)."""
    require_project(ctx, project)
    with ctx.sessionmaker() as session:
        files, total = models.list_files(
            session,
            project,
            path=path,
            series_id=series_id,
            latest_only=latest_only,
            state=state,
            storage_location=storage_location,
            content_date_from=content_date_from,
            content_date_to=content_date_to,
            include_deleted=include_deleted,
            deleted_only=deleted_only,
            ignored_from_all_rules=ignored_from_all_rules,
            limit=limit,
            offset=offset,
            order=order,
        )
        return FileList(
            items=[models.to_file_read(f) for f in files], total=total, limit=limit, offset=offset
        )

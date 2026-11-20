"""``GET /series/{series_id}/versions``: the versions of a series (API-09)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from heliopuddle.server import models
from heliopuddle.server.api.deps import (
    Ctx,
    requires,
    series_project,
)
from heliopuddle.server.auth.roles import ROLE_READER
from heliopuddle.server.schemas import (
    SeriesVersions,
)
from heliopuddle.versioning import display_series_key

router = APIRouter()


@router.get(
    "/series/{series_id}/versions",
    response_model=SeriesVersions,
    dependencies=[Depends(requires(ROLE_READER, project_of=series_project))],
)
def series_versions(series_id: int, ctx: Ctx, include_deleted: bool = False) -> SeriesVersions:
    """Return the versions of a series, newest version first."""
    with ctx.sessionmaker() as session:
        try:
            series, project, files = models.list_versions(
                session, series_id, include_deleted=include_deleted
            )
        except models.NotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from None
        return SeriesVersions(
            series_id=series.id,
            series_key=display_series_key(series.series_key),
            project=project,
            version_type=series.version_type,  # type: ignore[arg-type]
            items=[models.to_file_read(f) for f in files],
        )

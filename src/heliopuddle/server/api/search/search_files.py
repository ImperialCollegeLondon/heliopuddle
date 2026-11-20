"""``GET /search/files``: search file paths across projects (API-16)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from heliopuddle.server import models
from heliopuddle.server.api.deps import Ctx, CurrentPrincipal, requires
from heliopuddle.server.schemas import SearchResults

router = APIRouter()


@router.get("/search/files", response_model=SearchResults, dependencies=[Depends(requires(None))])
def search_files(
    ctx: Ctx,
    principal: CurrentPrincipal,
    q: Annotated[
        str,
        Query(
            min_length=2,
            max_length=200,
            description="Words separated by spaces; each must occur in the path (literal, "
            "case-insensitive)",
        ),
    ],
    project: Annotated[
        list[str] | None,
        Query(description="Search only these projects (repeatable); each must be readable"),
    ] = None,
    latest_only: bool = False,
    include_deleted: bool = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SearchResults:
    """Search file paths across the projects the caller holds a role on.

    A named project that is unknown, or that the caller holds no role on, gives
    the same ``404`` (AUTH-08). Results are ranked by how well the first word
    matches the file name, then newest first.
    """
    words = models.search_words(q)
    if not words:
        raise HTTPException(
            status_code=422,
            detail=[{"loc": ["query", "q"], "msg": "q must contain a word", "type": "value_error"}],
        )
    if project:
        names = list(dict.fromkeys(project))
        for name in names:
            if name not in ctx.projects or not principal.has_any_role_on(name):
                raise HTTPException(status_code=404, detail=f"project {name!r} not found")
    else:
        names = [name for name in ctx.projects if principal.has_any_role_on(name)]
    with ctx.sessionmaker() as session:
        page = models.search_files(
            session,
            names,
            words,
            latest_only=latest_only,
            include_deleted=include_deleted,
            limit=limit,
            offset=offset,
        )
        return SearchResults(
            items=[models.to_file_read(f) for f in page.files],
            total=page.total,
            limit=limit,
            offset=offset,
            projects=page.per_project,
        )

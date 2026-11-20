"""File search (API-16) and the typeahead suggestions fragment (UI-14)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from heliopuddle.server.schemas import FileRead
from heliopuddle.server.ui.shared.context import Ui, UiContext
from heliopuddle.server.ui.shared.links import files_url

router = APIRouter()

SEARCH_PAGE = 50
SUGGEST_LIMIT = 8


def run_search(
    ui: UiContext, request: Request, q: str, project: str | None, limit: int, offset: int
) -> tuple[Any, list[tuple[str, list[FileRead]]], list[str]]:
    """Search through the client; returns the results, them grouped by project, and the projects."""
    with ui.client_for(request, None) as client:
        projects = [p.name for p in client.list_projects()]
        results = client.search(q, [project] if project else None, limit=limit, offset=offset)
    groups: dict[str, list[FileRead]] = {}
    for item in results.items:
        groups.setdefault(item.project, []).append(item)
    return results, list(groups.items()), projects


@router.get("/search", response_class=HTMLResponse, name="ui_search")
def search_page(
    request: Request, ui: Ui, q: str = "", project: str = "", offset: int = 0
) -> Response:
    """Search results grouped by project, with paging."""
    q = q.strip()[:200]
    project = project.strip()
    offset = max(offset, 0)
    results, groups, projects = None, [], []
    if len(q) >= 2:
        results, groups, projects = run_search(ui, request, q, project or None, SEARCH_PAGE, offset)
    else:
        with ui.client_for(request, None) as client:
            projects = [p.name for p in client.list_projects()]
    base = ui.url(request, "ui_search")

    def page_link(new_offset: int) -> str:
        return files_url(base, [("q", q), ("project", project), ("offset", new_offset or None)])

    total = results.total if results else 0
    return ui.render(
        request,
        "search/search.html",
        search_q=q,
        search_project=project,
        projects=projects,
        results=results,
        groups=groups,
        words=q.split(),
        first=offset + 1,
        last=offset + (len(results.items) if results else 0),
        prev_url=page_link(max(offset - SEARCH_PAGE, 0)) if offset > 0 else None,
        next_url=page_link(offset + SEARCH_PAGE) if offset + SEARCH_PAGE < total else None,
        widen_url=files_url(base, [("q", q)]),
        project_url=lambda p: files_url(base, [("q", q), ("project", p)]),
    )


@router.get("/search/suggest", response_class=HTMLResponse, name="ui_suggest")
def suggest(request: Request, ui: Ui, q: str = "", project: str = "") -> Response:
    """Typeahead suggestions as an HTML fragment for ``hp.js``."""
    q = q.strip()[:200]
    project = project.strip()
    if len(q) < 2:
        return HTMLResponse("")
    results, groups, _ = run_search(ui, request, q, project or None, SUGGEST_LIMIT, 0)
    return ui.render(
        request,
        "search/_suggest.html",
        search_q=q,
        search_project=project,
        results=results,
        groups=groups,
        words=q.split(),
        all_url=files_url(ui.url(request, "ui_search"), [("q", q), ("project", project)]),
        widen_url=files_url(ui.url(request, "ui_search"), [("q", q)]),
    )

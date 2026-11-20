"""The Files page: view tabs, filter chips, the table and the inspector panel (UI-06, UI-12)."""

from __future__ import annotations

from dataclasses import replace
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from heliopuddle.server.ui.pages.files.filters import FileFilters, filter_chips
from heliopuddle.server.ui.pages.files.lookup import inspector_context
from heliopuddle.server.ui.pages.files.paging import (
    DEFAULT_PAGE_SIZE,
    NO_COLUMNS,
    PAGE_SIZES,
    choose_columns,
    page_size,
)
from heliopuddle.server.ui.pages.files.tabs import VIEWS
from heliopuddle.server.ui.shared.context import Ui
from heliopuddle.server.ui.shared.links import files_url
from heliopuddle.server.ui.shared.summary import view_counts

router = APIRouter()


@router.get("/projects/{project}/files", response_class=HTMLResponse, name="ui_files")
def files_page(
    request: Request,
    ui: Ui,
    project: str,
    limit: int | None = None,
    offset: int = 0,
    columns: str | None = None,
    file: str | None = None,
) -> Response:
    """List a project's files with filters, paging and an optional inspector."""
    size = page_size(limit)
    offset = max(offset, 0)
    params = {k: request.query_params.getlist(k) for k in request.query_params}
    filters = FileFilters.parse(params)
    selected_id = int(file) if file and file.isdigit() else None
    with ui.client_for(request, project) as client:
        summary = client.project_summary()
        page = client.find(**filters.find_kwargs(), limit=size, offset=offset)
        inspector = None
        if selected_id is not None:
            inspector = inspector_context(client, project, selected_id)
        shell = ui.project_shell(request, client, project, summary)
    base = ui.url(request, "ui_files", project=project)
    chosen = request.query_params.getlist("col")
    shown = choose_columns(summary.storage_locations, ",".join(chosen) or columns)
    default_columns = choose_columns(summary.storage_locations, None)
    columns_param = (",".join(shown) or NO_COLUMNS) if shown != default_columns else None

    def link(
        f: FileFilters = filters,
        *,
        new_offset: int = 0,
        new_limit: int = size,
        selected: int | None = None,
    ) -> str:
        return files_url(
            base,
            [
                *f.query(),
                ("columns", columns_param),
                ("limit", new_limit if new_limit != DEFAULT_PAGE_SIZE else None),
                ("offset", new_offset or None),
                ("file", selected),
            ],
        )

    here = link(new_offset=offset)
    counts = view_counts(summary)
    tabs = [
        {
            "key": v.key,
            "label": v.label,
            "count": counts[v.key],
            "tone": v.tone,
            "url": link(replace(filters, view=v.key, states=())),
            "active": v.key == filters.view,
        }
        for v in VIEWS.values()
    ]
    keep = [
        *filters.query(),
        ("columns", columns_param),
        ("limit", size if size != DEFAULT_PAGE_SIZE else None),
    ]
    return ui.render(
        request,
        "files/files.html",
        project=project,
        tab="files",
        **shell,
        page=page,
        filters=filters,
        chips=filter_chips(filters, lambda f: link(f)),
        tabs=tabs,
        keep=[(k, str(v)) for k, v in keep if v is not None and v != ""],
        columns=shown,
        all_columns=[s.name for s in summary.storage_locations],
        limit=size,
        first=offset + 1,
        last=offset + len(page.items),
        prev_url=link(new_offset=max(offset - size, 0)) if offset > 0 else None,
        next_url=link(new_offset=offset + size) if offset + size < page.total else None,
        size_urls=[(s, link(new_limit=s)) for s in PAGE_SIZES],
        row_url=lambda f: link(new_offset=offset, selected=f.id),
        clear_url=link(FileFilters(view=filters.view)),
        here=here,
        here_query=urlsplit(link(new_offset=offset, selected=selected_id)).query,
        selected_id=selected_id,
        inspector=inspector,
        close_url=link(new_offset=offset),
    )

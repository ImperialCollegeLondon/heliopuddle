"""The inspector as an HTML fragment for the Files table's in-page swap."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from heliopuddle.server.ui.pages.files.lookup import inspector_context
from heliopuddle.server.ui.shared.context import Ui
from heliopuddle.server.ui.shared.forms import return_query

router = APIRouter()


@router.get(
    "/projects/{project}/files/{file_id}/inspector",
    response_class=HTMLResponse,
    name="ui_inspector",
)
def inspector(request: Request, ui: Ui, project: str, file_id: str, back: str = "") -> Response:
    """The inspector for one file; ``back`` is the Files query to return to."""
    context = None
    if file_id.isdigit():
        with ui.client_for(request, project) as client:
            context = inspector_context(client, project, int(file_id))
    if context is None:
        return HTMLResponse(
            '<p class="empty">This file is not in this project.</p>', status_code=404
        )
    query = return_query(back)
    close = ui.url(request, "ui_files", project=project)
    trimmed = return_query(query, drop="file")
    return ui.render(
        request,
        "files/_inspector.html",
        project=project,
        inspector=context,
        here_query=query,
        close_url=f"{close}?{trimmed}" if trimmed else close,
    )

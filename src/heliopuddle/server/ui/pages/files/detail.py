"""The full file page: badges, actions, copies, details, preview, versions, metadata."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from heliopuddle.server.ui.pages.files.fragment import preview_context
from heliopuddle.server.ui.pages.files.lookup import inspector_context
from heliopuddle.server.ui.shared.context import Ui

router = APIRouter()


@router.get(
    "/projects/{project}/files/{file_id}", response_class=HTMLResponse, name="ui_file_detail"
)
def file_detail(
    request: Request, ui: Ui, project: str, file_id: str, preview: str = ""
) -> Response:
    """One file's page; ``preview=1`` renders the content preview in the page."""
    not_found = f"File {file_id!r} not found in project {project!r}."
    if not file_id.isdigit():
        return ui.error_page(request, 404, "Error 404", not_found)
    with ui.client_for(request, project) as client:
        context = inspector_context(client, project, int(file_id))
        if context is None:
            return ui.error_page(request, 404, "Error 404", not_found)
        summary = client.project_summary()
        shell = ui.project_shell(request, client, project, summary)
        # Without JavaScript the "Show preview" link asks for the preview in the page;
        # with it, hp.js fetches the fragment after the page has loaded (UI-15).
        pv = preview_context(ui, client, context["file"]) if preview == "1" else None
    return ui.render(
        request, "files/file.html", project=project, tab="files", pv=pv, **shell, **context
    )

"""File actions: accept a copy and ignore a file from rules (UI-07, UI-08).

Each answers with a redirect to the Files view or the file page it was made from.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from heliopuddle.server.ui.shared.context import Ui, UiContext
from heliopuddle.server.ui.shared.errors import BadRequestError
from heliopuddle.server.ui.shared.forms import FormData, SameOrigin, form_bool, return_query

router = APIRouter()


def _back_to(
    ui: UiContext, request: Request, project: str, file_id: int, form: dict[str, str]
) -> str:
    if form.get("return_to") == "files":
        base = ui.url(request, "ui_files", project=project)
        query = return_query(form.get("return_query", ""))
        return f"{base}?{query}" if query else base
    return ui.url(request, "ui_file_detail", project=project, file_id=str(file_id))


@router.post(
    "/projects/{project}/files/{file_id}/accept",
    name="ui_accept_file",
    dependencies=[SameOrigin],
)
def accept_file(
    request: Request, ui: Ui, project: str, file_id: int, form: dict[str, str] = FormData
) -> Response:
    """Accept a file's copy as it is (clears ``unknown`` or ``missing``)."""
    not_found = f"File {file_id} not found in project {project!r}."
    with ui.client_for(request, project) as client:
        if client.get_file(file_id).project != project:
            return ui.error_page(request, 404, "Error 404", not_found)
        client.accept_file(file_id, form.get("storage_location") or None)
    return RedirectResponse(_back_to(ui, request, project, file_id, form), status_code=303)


@router.post(
    "/projects/{project}/files/{file_id}/rules-ignore",
    name="ui_rules_ignore",
    dependencies=[SameOrigin],
)
def rules_ignore(
    request: Request, ui: Ui, project: str, file_id: int, form: dict[str, str] = FormData
) -> Response:
    """Ignore a file from all rules, or include it again."""
    if "ignored" not in form:
        raise BadRequestError("Missing ignored value.")
    ignored = form_bool(form["ignored"], "ignored")
    with ui.client_for(request, project) as client:
        if client.get_file(file_id).project != project:
            return ui.error_page(request, 404, "Error 404", "File not found in this project.")
        client.set_rules_ignored(file_id, ignored)
    return RedirectResponse(_back_to(ui, request, project, file_id, form), status_code=303)

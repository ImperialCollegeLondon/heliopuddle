"""The content preview as an HTML fragment (UI-15), loaded after the file page."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from heliopuddle.client import HelioPuddleClient, HelioPuddleError
from heliopuddle.server.schemas import FileRead
from heliopuddle.server.ui.pages.files import preview as ui_preview
from heliopuddle.server.ui.pages.files.lookup import project_file
from heliopuddle.server.ui.shared.context import Ui, UiContext
from heliopuddle.server.ui.shared.fileinfo import downloadable

router = APIRouter()


def preview_context(ui: UiContext, client: HelioPuddleClient, file: FileRead) -> dict[str, Any]:
    """What the preview template needs: the API's preview, or why there is none."""
    context: dict[str, Any] = {
        "file": file,
        "preview": None,
        "unavailable": None,
        "image_ok": file.size <= ui_preview.MAX_IMAGE_BYTES,
    }
    if not downloadable(file):
        context["unavailable"] = "No readable copy to preview."
        return context
    try:
        context["preview"] = client.preview(file.id)
    except HelioPuddleError as exc:
        if ui.login_mode and exc.status_code == 401:
            raise
        detail = exc.detail if isinstance(exc.detail, str) else "The preview failed."
        context["unavailable"] = f"No preview: {detail}"
    except httpx.HTTPError:
        context["unavailable"] = "No preview: the API is unreachable."
    return context


@router.get(
    "/projects/{project}/files/{file_id}/preview",
    response_class=HTMLResponse,
    name="ui_preview",
)
def preview_fragment(request: Request, ui: Ui, project: str, file_id: str) -> Response:
    """The preview of one file as an HTML fragment."""
    file = None
    with ui.client_for(request, project) as client:
        if file_id.isdigit():
            file = project_file(client, project, int(file_id))
        if file is None:
            return HTMLResponse(
                '<p class="empty">This file is not in this project.</p>', status_code=404
            )
        pv = preview_context(ui, client, file)
    return ui.render(request, "files/_preview.html", project=project, pv=pv)

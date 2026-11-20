"""A file's bytes: the sandboxed image for the preview and the download (UI-15)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import IO
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse

from heliopuddle.client import HelioPuddleClient
from heliopuddle.server.ui.pages.files import preview as ui_preview
from heliopuddle.server.ui.pages.files.lookup import project_file
from heliopuddle.server.ui.shared.context import Ui

router = APIRouter()

_CHUNK = 1 << 20


def _chunks(stream: IO[bytes], client: HelioPuddleClient) -> Iterator[bytes]:
    """Read the stream in chunks, closing it and the client when the response ends."""
    try:
        while chunk := stream.read(_CHUNK):
            yield chunk
    finally:
        stream.close()
        client.close()


@router.get("/projects/{project}/files/{file_id}/raw", name="ui_raw")
def raw_image(request: Request, ui: Ui, project: str, file_id: int) -> Response:
    """An image's bytes for the preview's ``<img>``, sandboxed (UI-15)."""
    client = ui.client_for(request, project)
    try:
        file = project_file(client, project, file_id)
        media_type = ui_preview.image_media_type(file.name) if file else None
        if file is None or media_type is None:
            client.close()
            return ui.error_page(request, 404, "Error 404", "No such image in this project.")
        if file.size > ui_preview.MAX_IMAGE_BYTES:
            client.close()
            return ui.error_page(
                request,
                413,
                "Error 413",
                "This image is larger than 20 MiB, too large to show.",
            )
        stream = client.open(file_id)
    except FileNotFoundError:
        client.close()
        return ui.error_page(request, 404, "Error 404", "This file has no available copy.")
    except Exception:
        client.close()
        raise
    return StreamingResponse(
        _chunks(stream, client),
        media_type=media_type,
        headers={
            "Content-Security-Policy": "sandbox; default-src 'none'; style-src 'unsafe-inline'",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f"inline; filename*=UTF-8''{quote(file.name, safe='')}",
            "Content-Length": str(file.size),
            "Cache-Control": "private, max-age=300",
        },
    )


@router.get("/projects/{project}/files/{file_id}/download", name="ui_download")
def download(request: Request, ui: Ui, project: str, file_id: int) -> Response:
    """Stream the file's content through ``client.open()`` as an attachment."""
    client = ui.client_for(request, project)
    try:
        file = client.get_file(file_id)
        if file.project != project:
            client.close()
            return ui.error_page(request, 404, "Error 404", "File not found in this project.")
        stream = client.open(file_id)
    except FileNotFoundError:
        client.close()
        return ui.error_page(request, 404, "Error 404", "This file has no available copy.")
    except Exception:
        client.close()
        raise
    disposition = f"attachment; filename*=UTF-8''{quote(file.name, safe='')}"
    return StreamingResponse(
        _chunks(stream, client),
        media_type="application/octet-stream",
        headers={"Content-Disposition": disposition, "Content-Length": str(file.size)},
    )

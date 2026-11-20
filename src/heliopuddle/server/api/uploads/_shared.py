"""Helpers shared by several ``/uploads`` endpoints."""

from __future__ import annotations

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from heliopuddle.server import models
from heliopuddle.server.api.deps import Ctx
from heliopuddle.server.schemas import FileRead


def read_file(ctx: Ctx, file_id: int) -> FileRead:
    """Return a reserved file as an API record."""
    with ctx.sessionmaker() as session:
        file = models.get_file(session, file_id)
        if file is None:  # pragma: no cover - reservations hold their file
            raise HTTPException(status_code=404, detail=f"file {file_id} not found")
        return models.to_file_read(file)


def not_current(e: Exception) -> JSONResponse:
    """A ``409`` for a token that is no longer the path's current operation."""
    return JSONResponse(status_code=409, content={"detail": str(e)})

"""Helpers shared by several ``/files`` endpoints."""

from __future__ import annotations

from fastapi import HTTPException

from heliopuddle.server import models
from heliopuddle.server.api.deps import Ctx
from heliopuddle.server.schemas import FileRead


def invalid_path(message: str, loc: str = "body") -> HTTPException:
    """A ``422`` for an invalid path, located in ``loc`` (``body`` or ``query``)."""
    return HTTPException(
        status_code=422,
        detail=[{"loc": [loc, "path"], "msg": message, "type": "value_error"}],
    )


def validate_path(path: str, loc: str = "body") -> None:
    """Raise a ``422`` unless ``path`` is a valid relative store path."""
    try:
        models.validate_path(path)
    except ValueError as e:
        raise invalid_path(str(e), loc) from None


def read_file(ctx: Ctx, file_id: int) -> FileRead:
    """Return the file as an API record, or raise ``404``."""
    with ctx.sessionmaker() as session:
        file = models.get_file(session, file_id)
        if file is None:
            raise HTTPException(status_code=404, detail=f"file {file_id} not found")
        return models.to_file_read(file)

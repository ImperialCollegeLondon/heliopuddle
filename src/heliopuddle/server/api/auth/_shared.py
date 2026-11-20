"""Helpers shared by the ``/auth`` endpoints."""

from __future__ import annotations

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from heliopuddle.server.api.deps import Ctx

NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
LOCAL_ONLY = "this endpoint exists only when HELIOPUDDLE_AUTH_MODE is local"


def require_local(ctx: Ctx) -> None:
    """404 unless the auth mode is ``local``."""
    if ctx.settings.auth_mode != "local":
        raise HTTPException(status_code=404, detail=LOCAL_ONLY)


def oauth_error(error: str, description: str) -> JSONResponse:
    """An OAuth2 ``400`` error body (``no-store``)."""
    return JSONResponse(
        status_code=400,
        content={"error": error, "error_description": description},
        headers=NO_STORE,
    )

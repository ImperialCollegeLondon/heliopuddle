"""``GET /version``."""

from __future__ import annotations

from fastapi import APIRouter

import heliopuddle
from heliopuddle.server.schemas import VersionResponse

router = APIRouter()


@router.get("/version", response_model=VersionResponse)
def version() -> VersionResponse:
    """Return the package version."""
    return VersionResponse(version=heliopuddle.__version__)

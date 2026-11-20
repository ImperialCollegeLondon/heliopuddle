"""``GET /health`` and ``GET /version`` schemas.

This module must not import SQLAlchemy or anything from ``heliopuddle.server``
other than the schema modules: the client imports it.
"""

from __future__ import annotations

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """``GET /health``. ``status`` is ``ok`` only when every part is ``ok``."""

    status: str
    database: str
    prefect: str


class VersionResponse(BaseModel):
    """``GET /version``."""

    version: str

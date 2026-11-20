"""``GET /health``: is the database and Prefect reachable (API-06)."""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from prefect.settings import PREFECT_API_URL
from sqlalchemy import text

from heliopuddle.server.api.deps import Ctx
from heliopuddle.server.schemas import HealthResponse

router = APIRouter()
logger = logging.getLogger(__name__)

PREFECT_HEALTH_TIMEOUT = 3.0


def prefect_api_url() -> str | None:
    """Return the Prefect API URL this process uses (``PREFECT_API_URL``)."""
    return PREFECT_API_URL.value()


def _check_database(ctx: Ctx) -> str:
    try:
        with ctx.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        # The route is unauthenticated: details go to the log, not to the caller.
        logger.warning("health: the database check failed", exc_info=True)
        return "error"
    return "ok"


def _check_prefect() -> str:
    url = prefect_api_url()
    if not url:
        logger.warning("health: PREFECT_API_URL is not set")
        return "error"
    try:
        response = httpx.get(f"{url.rstrip('/')}/health", timeout=PREFECT_HEALTH_TIMEOUT)
    except httpx.HTTPError as e:
        logger.warning("health: Prefect at %s is unreachable: %s", url, type(e).__name__)
        return "error"
    if response.status_code != 200:
        logger.warning("health: Prefect at %s returned %s", url, response.status_code)
        return "error"
    return "ok"


@router.get("/health", response_model=HealthResponse, responses={503: {"model": HealthResponse}})
def health(ctx: Ctx) -> JSONResponse:
    """Report whether the database and Prefect are reachable (200, else 503)."""
    database = _check_database(ctx)
    prefect = _check_prefect()
    ok = database == "ok" and prefect == "ok"
    body = HealthResponse(status="ok" if ok else "error", database=database, prefect=prefect)
    return JSONResponse(status_code=200 if ok else 503, content=body.model_dump())

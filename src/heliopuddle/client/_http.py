"""HTTP helpers of the client: error mapping, ``Retry-After`` and server time."""

from __future__ import annotations

import contextlib
import email.utils
import math
from datetime import UTC, datetime
from typing import Any

import httpx

from heliopuddle.client.errors import (
    AuthenticationError,
    BusyError,
    ConflictError,
    HelioPuddleError,
    NotFoundError,
    PermissionDeniedError,
)
from heliopuddle.server.schemas import FileRead

CLOCK_SKEW_TOLERANCE = 2.0
"""Seconds of difference between client and server clocks that are ignored."""


def retry_after(value: str) -> float | None:
    """Parse a ``Retry-After`` header: delay seconds or an HTTP date; None if unusable."""
    try:
        delay = float(value)
        return max(0.0, delay) if math.isfinite(delay) else None
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, (aware(when) - datetime.now(UTC)).total_seconds())


def api_error(response: httpx.Response) -> HelioPuddleError:
    try:
        body = response.json()
    except ValueError:
        body = None
    detail = body.get("detail", response.text) if isinstance(body, dict) else response.text
    if response.status_code == 409 and isinstance(body, dict) and "file" in body:
        return ConflictError(detail, FileRead.model_validate(body["file"]))
    if response.status_code == 409 and "retry-after" in response.headers:
        return BusyError(detail, retry_after(response.headers["retry-after"]))
    if response.status_code == 401:
        return AuthenticationError(401, detail)
    if response.status_code == 403:
        return PermissionDeniedError(forbidden_detail(detail), required_roles(body))
    error = NotFoundError if response.status_code == 404 else HelioPuddleError
    return error(response.status_code, detail)


def required_roles(body: Any) -> list[str]:
    """The ``required_roles`` of a ``403`` body (top level, or inside ``detail``)."""
    if not isinstance(body, dict):
        return []
    roles = body.get("required_roles")
    if roles is None and isinstance(body.get("detail"), dict):
        roles = body["detail"].get("required_roles")
    return [r for r in roles if isinstance(r, str)] if isinstance(roles, list) else []


def forbidden_detail(detail: Any) -> Any:
    if isinstance(detail, dict) and "detail" in detail:
        return detail["detail"]
    return detail


def server_now(response: httpx.Response) -> datetime:
    """The time to measure a reservation's deadline from.

    The local clock, unless the server's ``Date`` header differs from it by
    more than :data:`CLOCK_SKEW_TOLERANCE` seconds (then the server's clock).
    ``Date`` has one-second resolution, so using it when the clocks agree would
    distort short leases by up to a second.
    """
    local = datetime.now(UTC)
    with contextlib.suppress(TypeError, ValueError):
        server = aware(email.utils.parsedate_to_datetime(response.headers.get("date", "")))
        if abs((server - local).total_seconds()) > CLOCK_SKEW_TOLERANCE:
            return server
    return local


def aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)

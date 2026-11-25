"""Error responses and handlers shared by the routers (busy, invalid version, conflicts, rclone)."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from heliopuddle.server import models
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import utcnow
from heliopuddle.server.schemas import (
    Busy,
    FileConflict,
    FileRead,
    StorageLocationIncompleteError,
)
from heliopuddle.server.services.rule_control import (
    ReconcileBusyError,
    RuleBusyError,
    RuleDisabledError,
)
from heliopuddle.server.services.runs import RunSubmitError
from heliopuddle.settings import Settings
from heliopuddle.storage.credentials import StorageLocationIncomplete
from heliopuddle.storage.rclone import RcloneBusyError, wait_limit
from heliopuddle.versioning import InvalidVersionError


def check_upload_size(settings: Settings, size: int) -> None:
    """Refuse a declared file size above ``max_upload_size`` (API-23).

    Raises:
        HTTPException: 413 with the limit in the message.
    """
    limit = int(settings.max_upload_size)
    if size > limit:
        raise HTTPException(
            status_code=413,
            detail=f"declared size {size} bytes exceeds the server's limit of {limit} bytes "
            "(HELIOPUDDLE_MAX_UPLOAD_SIZE)",
        )


@contextmanager
def rule_errors() -> Iterator[None]:
    """Map rule lookup, disabled-state and Prefect errors to their HTTP contract."""
    try:
        yield
    except models.NotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from None
    except RuleDisabledError as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    except RuleBusyError as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    except ReconcileBusyError as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    except RunSubmitError as error:
        raise HTTPException(status_code=503, detail=str(error)) from None


def busy(copy_id: int | None, deadline: datetime | None, detail: str | None = None) -> JSONResponse:
    """A ``409 busy`` with ``Retry-After`` (seconds until the deadline, at least 1)."""
    retry = 1
    if deadline is not None:
        retry = max(1, math.ceil((deadline - utcnow()).total_seconds()))
    body = Busy(
        detail=detail or "another operation owns this path; retry later",
        copy_id=copy_id,
        deadline=deadline,
    )
    return JSONResponse(
        status_code=409,
        content=body.model_dump(mode="json"),
        headers={"Retry-After": str(retry)},
    )


def conflict(detail: str, file: FileRead) -> JSONResponse:
    """A ``409`` with the existing record (:class:`FileConflict`)."""
    body = FileConflict(detail=detail, file=file)
    return JSONResponse(status_code=409, content=body.model_dump(mode="json"))


def version_kind_mismatch(e: models.VersionKindMismatch) -> HTTPException:
    """A ``422`` naming the series' kind (API-21)."""
    return HTTPException(
        status_code=422,
        detail=[
            {
                "loc": ["body", f"version_{e.version_type}"],
                "msg": str(e),
                "type": "value_error",
                "ctx": {"series_version_type": e.series_type, "version_type": e.version_type},
            }
        ],
    )


def invalid_version(e: InvalidVersionError, loc: str = "body") -> HTTPException:
    """A ``422`` naming the version rule and the value (DEP-12)."""
    return HTTPException(
        status_code=422,
        detail=[
            {
                "loc": [loc, "path"],
                "msg": str(e),
                "type": "value_error",
                "ctx": {"rule_index": e.rule_index, "group": e.group, "value": e.value},
            }
        ],
    )


_SECRET_SEGMENTS = frozenset({"users", "auth"})


def _scrub_validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    errors = exc.errors()
    if _SECRET_SEGMENTS & set(request.url.path.split("/")):
        errors = [{k: v for k, v in e.items() if k not in ("input", "ctx")} for e in errors]
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})


def install_validation_scrub(app: FastAPI) -> None:
    """Drop the echoed ``input`` from ``422`` bodies of ``/users`` and ``/auth`` requests.

    FastAPI repeats the rejected input in validation errors; on these routes that
    would send a rejected password back (and into proxy logs).
    """
    app.add_exception_handler(RequestValidationError, _scrub_validation_error)


def _storage_location_incomplete(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StorageLocationIncomplete)
    body = StorageLocationIncompleteError(
        detail=f"storage location incomplete: {exc}", problems=exc.problems
    )
    return JSONResponse(status_code=409, content=body.model_dump(mode="json"))


def install_storage_location_incomplete(app: FastAPI) -> None:
    """Make :class:`StorageLocationIncomplete` answer ``409 storage_location_incomplete``.

    The body is :class:`StorageLocationIncompleteError`: ``detail``,
    ``reason: "storage_location_incomplete"`` and the ``problems`` (STO-18).
    """
    app.add_exception_handler(StorageLocationIncomplete, _storage_location_incomplete)


def _rclone_busy(request: Request, exc: Exception) -> JSONResponse:
    wait = request.app.state.rclone_wait()
    return JSONResponse(
        status_code=503,
        content={"detail": f"rclone storage location busy: {exc}", "reason": "rclone_busy"},
        headers={"Retry-After": str(max(1, math.ceil(wait)))},
    )


class RcloneWaitMiddleware:
    """Bound how long a request waits for a busy rclone config (STO-20).

    Services wait without limit; a request waits ``HELIOPUDDLE_RCLONE_API_WAIT``
    and then gets ``503`` with ``Retry-After`` (:func:`install_rclone_busy`).
    """

    def __init__(self, app: ASGIApp, wait: Callable[[], float]) -> None:
        self.app = app
        self.wait = wait

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        with wait_limit(self.wait()):
            await self.app(scope, receive, send)


def install_rclone_busy(app: FastAPI, wait: Callable[[], float]) -> None:
    """Bound rclone config waits per request and answer ``503`` when one is busy."""
    app.state.rclone_wait = wait
    app.add_middleware(RcloneWaitMiddleware, wait=wait)
    app.add_exception_handler(RcloneBusyError, _rclone_busy)


def require_covered(ctx: AppContext, project: str, storage_location: str, path: str) -> None:
    """``422`` when ``path`` is outside the storage location's ``path_prefix`` (STO-23).

    Checked before any write: such a storage location holds only one project
    folder, so a copy elsewhere cannot exist. An unknown storage location is
    left to the caller's own ``404``.
    """
    cfg = ctx.storage_location_config(project, storage_location)
    if cfg is None or not cfg.path_prefix or path.startswith(cfg.path_prefix + "/"):
        return
    raise HTTPException(
        status_code=422,
        detail=[
            {
                "loc": ["body", "path"],
                "msg": f"storage location {storage_location!r} holds only paths below "
                f"{cfg.path_prefix + '/'!r}",
                "type": "value_error",
            }
        ],
    )

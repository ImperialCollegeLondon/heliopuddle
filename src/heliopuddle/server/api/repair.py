"""Run a write that may need a repair first, and map its errors (register and reserve)."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from heliopuddle.server import models
from heliopuddle.server.api.deps import Ctx
from heliopuddle.server.api.errors import (
    busy,
    conflict,
    invalid_version,
    version_kind_mismatch,
)
from heliopuddle.server.schemas import FileRead
from heliopuddle.server.services.upload import RepairResult, repair_expired
from heliopuddle.versioning import InvalidVersionError

T = TypeVar("T")


def attempt_write(
    ctx: Ctx,
    attempt: Callable[[], T],
    read_file: Callable[[Ctx, int], FileRead],
    *,
    conflict_detail: Callable[[models.ConflictError], str] = str,
    also_conflicts: tuple[type[Exception], ...] = (),
) -> T | JSONResponse:
    """Call ``attempt`` until it succeeds, repairing an expired operation in its way.

    A copy holding an expired operation is repaired once, then ``attempt`` runs
    again; a repair that stays incomplete answers ``409 busy``.

    Args:
        ctx: the request context.
        attempt: one try of the write (it opens its own transaction).
        read_file: loads the existing file for a conflict answer.
        conflict_detail: the message for a different file at the path.
        also_conflicts: further errors with a ``file_id`` that answer ``409`` with
            the existing file.

    Returns:
        ``attempt``'s result, or the error response.

    Raises:
        HTTPException: ``404`` for an unknown or inactive project or storage
            location; ``422`` for an invalid or mismatched version.
    """
    repaired: set[int] = set()
    while True:
        try:
            return attempt()
        except models.NeedsRepair as e:
            if e.copy_id in repaired or repair_expired(e.copy_id) == RepairResult.INCOMPLETE:
                return busy(e.copy_id, None, "an earlier operation on this path awaits repair")
            repaired.add(e.copy_id)
        except models.CopyBusy as e:
            return busy(e.copy_id, e.deadline)
        except InvalidVersionError as e:
            raise invalid_version(e) from None
        except models.VersionKindMismatch as e:
            raise version_kind_mismatch(e) from None
        except models.VersionImmutable as e:
            return conflict(str(e), read_file(ctx, e.file_id))
        except (models.NotFoundError, models.InactiveError) as e:
            raise HTTPException(status_code=404, detail=str(e)) from None
        except models.ConflictError as e:
            return conflict(conflict_detail(e), read_file(ctx, e.file_id))
        except also_conflicts as e:
            return conflict(str(e), read_file(ctx, e.file_id))  # type: ignore[attr-defined]

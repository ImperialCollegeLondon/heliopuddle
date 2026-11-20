"""Accept an ``unknown`` copy as it is now: the guarded write step (API-11, SVC-11).

See docs/design/operations.md ("Accept an unknown copy"). The copy is chosen
with :func:`heliopuddle.server.models.index.get_accept_target`; its generation
is read with the choice, and the write applies only if the copy is still
``unknown`` at that generation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from heliopuddle.server.database.orm import File, FileStorageLocation, utcnow
from heliopuddle.server.models import transitions as tr
from heliopuddle.server.models.files import NotFoundError
from heliopuddle.server.models.index import (
    AcceptTarget,
    AmbiguousCopyError,
    NotUnknownError,
    get_accept_target,
)
from heliopuddle.server.models.series import lock_series_by_id, recompute_latest
from heliopuddle.server.schemas import FileState


@dataclass(frozen=True)
class GuardedAcceptTarget:
    """The copy to accept and the generation it had when chosen."""

    target: AcceptTarget
    generation: int


def choose(
    session: Session, file_id: int, storage_location: str | None = None
) -> GuardedAcceptTarget:
    """Choose the ``unknown`` copy to accept and read its generation.

    Raises:
        NotFoundError: the file does not exist or is deleted, or the project
            has no storage location of that name.
        AmbiguousCopyError: no storage location given and not exactly one
            ``unknown`` copy.
        NotUnknownError: the copy in that storage location is not ``unknown``.
    """
    target = get_accept_target(session, file_id, storage_location)
    generation = session.get_one(FileStorageLocation, target.copy_id).generation
    return GuardedAcceptTarget(target, generation)


def accept_copy_record(
    session: Session,
    chosen: GuardedAcceptTarget,
    *,
    size: int,
    mtime: datetime | None,
    hash: str,
    user: str | None,
) -> File:
    """Accept the chosen copy with what was observed, if it is unchanged since it was chosen.

    Under the series lock, the copy must still be ``unknown`` at the chosen
    generation and the file live. Then the file takes the observed hash and
    size (``updated_by`` = ``user``); the copy becomes ``available`` with the
    observation and its generation is bumped; when the hash changed the
    file's other copies hold the old content and become ``unknown`` (except
    those owned by an operation, which settles against the new hash itself);
    ``is_latest`` is recomputed.

    Raises:
        NotFoundError: the file was deleted meanwhile.
        NotUnknownError: the copy changed since it was chosen.
        CopyBusy: another operation owns the copy.
    """
    target = chosen.target
    try:
        copy = tr.lock_copy(
            session,
            target.copy_id,
            generation=chosen.generation,
            states={FileState.UNKNOWN},
            require_live=False,
        )
    except tr.StaleOperation as e:
        if session.get_one(File, target.file_id).deleted_at is not None:
            raise NotFoundError(f"file {target.file_id} not found") from e
        raise NotUnknownError(f"file {target.file_id}: the copy changed: {e}") from e
    file = session.get_one(File, target.file_id)
    if file.deleted_at is not None:  # pragma: no cover - a deleted file has no unknown copy
        raise NotFoundError(f"file {target.file_id} not found")
    now = utcnow()
    hash = hash.lower()
    if file.hash != hash:
        for other in file.copies:
            if (
                other.id != copy.id
                and other.state not in (FileState.DELETED, FileState.UNKNOWN)
                and other.op_token is None  # an operation's copy is settled by that operation
            ):
                other.state = FileState.UNKNOWN
                other.updated_at = now
                tr.bump(other)
    file.hash = hash
    file.size = size
    file.updated_by = user
    file.updated_at = now
    copy.state = FileState.AVAILABLE
    copy.size = size
    copy.mtime = mtime
    copy.hash = hash
    copy.last_seen_at = now
    copy.updated_at = now
    tr.bump(copy)
    recompute_latest(session, lock_series_by_id(session, file.series_id))
    return file


__all__ = [
    "AmbiguousCopyError",
    "GuardedAcceptTarget",
    "NotUnknownError",
    "accept_copy_record",
    "choose",
]

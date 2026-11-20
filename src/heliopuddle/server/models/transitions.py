"""Guarded copy transitions: operation ownership and expected revisions.

The contract is docs/design/operations.md ("Ownership", "Guarded
transitions"). Every copy state change goes through these helpers so that an
operation acting on stale information (an older generation, a deleted file, a
copy owned by someone else) is detected under the series lock and dropped.

Lock order is always the series row, then the copy (as every writer does).
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from heliopuddle.server.database.orm import File, FileStorageLocation, utcnow
from heliopuddle.server.models.series import LATEST_ELIGIBLE_STATES, lock_series_of_file
from heliopuddle.server.schemas import FileState

__all__ = [
    "LATEST_ELIGIBLE_STATES",
    "CopyBusy",
    "NeedsRepair",
    "StaleOperation",
    "bump",
    "check_free",
    "clock",
    "is_expired",
    "is_latest_eligible",
    "is_owned",
    "lock_copy",
    "release",
    "take",
]


def clock() -> datetime:
    """The current time used for ownership decisions (patched in tests)."""
    return utcnow()


class StaleOperation(Exception):
    """The copy changed since the operation read it; the change was not applied."""


class CopyBusy(Exception):
    """Another live operation owns the copy (``409 busy``)."""

    def __init__(self, copy_id: int, deadline: datetime | None) -> None:
        super().__init__(f"copy {copy_id} is owned by another operation until {deadline}")
        self.copy_id = copy_id
        self.deadline = deadline


class NeedsRepair(Exception):
    """The copy holds an expired operation (or a failed delete) that must be repaired first.

    Attributes:
        copy_id: the copy to pass to ``repair_expired``.
    """

    def __init__(self, copy_id: int) -> None:
        super().__init__(f"copy {copy_id} holds an expired operation awaiting repair")
        self.copy_id = copy_id


def check_free(copy: FileStorageLocation, now: datetime) -> None:
    """Raise unless ``copy`` is free for a new operation.

    Raises:
        CopyBusy: a live operation owns it.
        NeedsRepair: it holds an expired operation, or is ``deleting``
            without a token (a delete that did not finish).
    """
    if is_owned(copy, now):
        raise CopyBusy(copy.id, copy.op_deadline)
    if copy.op_token is not None or copy.state == FileState.DELETING:
        raise NeedsRepair(copy.id)


def is_owned(copy: FileStorageLocation, now: datetime) -> bool:
    """Return True if a live (unexpired) operation owns ``copy``.

    Args:
        copy: the copy.
        now: the current time.
    """
    return copy.op_token is not None and copy.op_deadline is not None and copy.op_deadline > now


def is_expired(copy: FileStorageLocation, now: datetime) -> bool:
    """Return True if ``copy`` holds a token whose deadline has passed.

    Args:
        copy: the copy.
        now: the current time.
    """
    return copy.op_token is not None and not is_owned(copy, now)


def is_latest_eligible(copy: FileStorageLocation, now: datetime) -> bool:
    """Return True if this copy makes its (live) file latest-eligible.

    ``available`` and ``overwriting`` copies serve verified content;
    ``pending`` is eligible only without a token (known content awaiting
    verification). A token-holding ``pending`` copy is an unpublished
    new-file upload and is never eligible, expired or not, so that
    ``is_latest`` never depends on the clock (``now`` is accepted for
    symmetry with :func:`is_owned`).

    Args:
        copy: the copy.
        now: the current time.
    """
    del now
    if copy.state in LATEST_ELIGIBLE_STATES:
        return True
    return copy.state == FileState.PENDING and copy.op_token is None


def lock_copy(
    session: Session,
    copy_id: int,
    *,
    generation: int | None = None,
    states: Collection[FileState] | None = None,
    expected_hash: str | None = None,
    token: str | None = None,
    require_live: bool = True,
) -> FileStorageLocation:
    """Lock the copy's series (and the copy) and check the expectations.

    Ownership is checked first: when the copy holds a token other than
    ``token``, a live one raises :class:`CopyBusy`; an expired one, or a
    missing token when ``token`` was given (the operation was taken over or
    already finished), raises :class:`StaleOperation`.

    Args:
        session: a write session.
        copy_id: the copy.
        generation: the generation the operation started from, if any.
        states: allowed current states, if restricted.
        expected_hash: the file hash the operation expects, if any.
        token: the operation's own token; a different live token raises
            :class:`CopyBusy`.
        require_live: the file must not be soft-deleted.

    Returns:
        The locked copy (its ``file`` is loaded and current).

    Raises:
        StaleOperation: if any expectation does not hold.
        CopyBusy: if another live operation owns the copy.
    """
    file_id = session.scalar(
        select(FileStorageLocation.file_id).where(FileStorageLocation.id == copy_id)
    )
    if file_id is None:
        raise StaleOperation(f"copy {copy_id} no longer exists")
    lock_series_of_file(session, file_id)
    copy = session.get(FileStorageLocation, copy_id, with_for_update=True, populate_existing=True)
    if copy is None:  # pragma: no cover - deleted between the two reads, under no lock
        raise StaleOperation(f"copy {copy_id} no longer exists")
    file = session.get_one(File, copy.file_id, populate_existing=True)
    if copy.op_token != token:
        if copy.op_token is not None and is_owned(copy, clock()):
            raise CopyBusy(copy_id, copy.op_deadline)
        raise StaleOperation(
            f"copy {copy_id}: operation token is no longer current"
            if token is not None
            else f"copy {copy_id} holds an expired operation awaiting repair"
        )
    if require_live and file.deleted_at is not None:
        raise StaleOperation(f"copy {copy_id}: file {file.id} was deleted")
    if states is not None and copy.state not in states:
        raise StaleOperation(f"copy {copy_id} is {copy.state.value}")
    if generation is not None and copy.generation != generation:
        raise StaleOperation(
            f"copy {copy_id} generation is {copy.generation}, expected {generation}"
        )
    if expected_hash is not None and file.hash != expected_hash.lower():
        raise StaleOperation(f"copy {copy_id}: the file hash changed")
    return copy


def take(
    copy: FileStorageLocation, state: FileState, token: str, deadline: datetime
) -> FileStorageLocation:
    """Give ``copy`` to an operation: set its state, token and deadline.

    Args:
        copy: a copy locked with :func:`lock_copy`.
        state: the in-flight state (``pending``, ``overwriting``, ``deleting``).
        token: the operation's token.
        deadline: when ownership expires.
    """
    copy.state = state
    copy.op_token = token
    copy.op_deadline = deadline
    copy.published_token = None
    copy.move_destination_id = None
    copy.updated_at = utcnow()
    return copy


def release(copy: FileStorageLocation, state: FileState) -> FileStorageLocation:
    """End ownership of ``copy``: set its state and clear the token and pending content.

    Args:
        copy: a copy locked with :func:`lock_copy`.
        state: the state the operation leaves the copy in.
    """
    copy.state = state
    copy.op_token = None
    copy.op_deadline = None
    copy.pending_hash = None
    copy.pending_size = None
    copy.pending_metadata = None
    copy.pending_content_date = None
    copy.pending_by = None
    copy.pending_staged = False
    if state != FileState.DELETING:
        # A failed move delete stays ``deleting`` and keeps its destination guard.
        copy.move_destination_id = None
    copy.updated_at = utcnow()
    return copy


def bump(copy: FileStorageLocation) -> None:
    """Record a content-changing transition (increment ``generation``)."""
    copy.generation = (copy.generation or 0) + 1
    copy.published_token = None

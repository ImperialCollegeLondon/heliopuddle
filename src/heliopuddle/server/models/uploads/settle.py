"""Settle an upload or overwrite once the store step is done, and find abandoned ones."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from heliopuddle.server.database.orm import File, FileStorageLocation, utcnow
from heliopuddle.server.models import transitions as tr
from heliopuddle.server.models.series import (
    lock_series_by_id,
    lock_series_of_file,
    recompute_latest,
)
from heliopuddle.server.models.snapshots import CopySnapshot, copy_snapshot
from heliopuddle.server.models.uploads.lifecycle import _UPLOAD_STATES
from heliopuddle.server.models.uploads.types import new_token
from heliopuddle.server.schemas import FileState


def unclaim(session: Session, snap: CopySnapshot, token: str, lease: timedelta) -> None:
    """Hand a claimed upload back to its client (its token and a fresh deadline).

    Used when completing could not observe the object: the reservation stays
    owned, ``state`` and ``generation`` unchanged, so the client can retry.

    Raises:
        StaleOperation: the claim is no longer this operation's (a repair took it).
    """
    copy = _lock_owned(session, snap, _UPLOAD_STATES)
    copy.op_token = token
    copy.op_deadline = tr.clock() + lease


def take_over_expired(
    session: Session, copy_id: int, now: datetime, lease: timedelta
) -> tuple[CopySnapshot, str | None] | None:
    """Give an abandoned operation's copy to a repair.

    A copy whose token expired at ``now``, or a ``deleting`` copy without a
    token (a delete that failed), gets a fresh token and deadline.

    Returns:
        The snapshot (with the repair's token) and the expired token, or None
        if the copy does not exist, has no expired operation, or is owned by a
        live one.
    """
    file_id = session.scalar(
        select(FileStorageLocation.file_id).where(FileStorageLocation.id == copy_id)
    )
    if file_id is None:
        return None
    lock_series_of_file(session, file_id)
    copy = session.get(FileStorageLocation, copy_id, with_for_update=True, populate_existing=True)
    if copy is None:  # pragma: no cover - copies are removed only under the series lock
        return None
    if copy.op_token is None:
        if copy.state != FileState.DELETING:
            return None
    elif tr.is_owned(copy, now):
        return None
    old = copy.op_token
    copy.op_token = new_token()
    copy.op_deadline = tr.clock() + lease
    session.flush()
    return copy_snapshot(session, copy), old


def _lock_owned(session: Session, snap: CopySnapshot, states: frozenset[FileState]) -> Any:
    return tr.lock_copy(
        session,
        snap.copy_id,
        token=snap.token,
        generation=snap.generation,
        states=states,
    )


def _observe(
    copy: FileStorageLocation, size: int, mtime: datetime | None, hash: str | None
) -> None:
    copy.size = size
    copy.mtime = mtime
    copy.hash = hash.lower() if hash else None
    copy.last_seen_at = utcnow()


def finish_new(
    session: Session,
    snap: CopySnapshot,
    *,
    size: int | None,
    mtime: datetime | None,
    hash: str | None,
    published_token: str | None = None,
) -> FileState:
    """Complete a new-file upload: ``available`` if the object matches, else ``unknown``.

    The object matches when its hash equals both the reserved hash and the
    file's hash, and its size the file's size. The token is cleared, the
    generation bumped and ``is_latest`` recomputed.

    Args:
        session: a session in a write transaction; the caller commits.
        snap: the claimed upload.
        size: the observed size, or None if the object is missing.
        mtime: the observed mtime.
        hash: the observed md5, or None if missing.
        published_token: set when a repair settles an upload whose client
            token this is, so that client's late complete can be answered
            (see :func:`published_by_repair`).

    Returns:
        The copy's new state.

    Raises:
        StaleOperation: the upload is no longer this operation's.
    """
    copy = _lock_owned(session, snap, frozenset({FileState.PENDING}))
    file = session.get_one(File, copy.file_id)
    match = (
        hash is not None
        and hash.lower() == (copy.pending_hash or "")
        and hash.lower() == file.hash
        and size == file.size
    )
    if size is not None:
        _observe(copy, size, mtime, hash if match else None)
    state = FileState.AVAILABLE if match else FileState.UNKNOWN
    tr.release(copy, state)
    tr.bump(copy)
    if state == FileState.AVAILABLE:
        copy.published_token = published_token
        if snap.created_by_reservation:
            # A new copy arrives when its upload completes, not when it is reserved.
            copy.arrived_at = utcnow()
    recompute_latest(session, lock_series_by_id(session, file.series_id))
    return state


def drop_new(session: Session, snap: CopySnapshot, *, object_present: bool) -> bool:
    """Drop an abandoned new-file reservation whose object does not match.

    A copy row the reservation created is deleted, with its file row when the
    file has no other copy (nothing was ever published); the caller has
    removed the object. A reservation on an existing copy leaves the row: the
    copy becomes ``unknown`` if an object is present (it is left in the
    store), else ``missing``.

    Returns:
        True if the copy row was deleted.

    Raises:
        StaleOperation: the reservation is no longer this operation's.
    """
    copy = _lock_owned(session, snap, frozenset({FileState.PENDING}))
    file = session.get_one(File, copy.file_id)
    series = lock_series_by_id(session, file.series_id)
    if not snap.created_by_reservation:
        tr.release(copy, FileState.UNKNOWN if object_present else FileState.MISSING)
        tr.bump(copy)
        recompute_latest(session, series)
        return False
    others = session.scalar(
        select(FileStorageLocation.id).where(
            FileStorageLocation.file_id == file.id, FileStorageLocation.id != copy.id
        )
    )
    session.expunge(copy)
    session.execute(delete(FileStorageLocation).where(FileStorageLocation.id == snap.copy_id))
    if others is None:
        session.expunge(file)
        session.execute(delete(File).where(File.id == snap.file_id))
    recompute_latest(session, series)
    return True


def finish_overwrite(
    session: Session,
    snap: CopySnapshot,
    *,
    size: int,
    mtime: datetime | None,
    hash: str,
    user: str | None = None,
    published_token: str | None = None,
) -> None:
    """Record a published overwrite (O2 step 4).

    The file takes the new hash and size (``updated_by`` = ``user`` when
    given), the copy becomes ``available`` with the observation, its token is
    cleared and generation bumped; the file's other copies hold the old
    content and become ``unknown`` (except those owned by an operation, which
    settles against the new file hash itself); ``is_latest`` is recomputed.

    Raises:
        StaleOperation: the overwrite is no longer this operation's.
    """
    copy = _lock_owned(session, snap, frozenset({FileState.OVERWRITING}))
    file = session.get_one(File, copy.file_id)
    now = utcnow()
    for other in file.copies:
        if (
            other.id != copy.id
            and other.state not in (FileState.DELETED, FileState.UNKNOWN)
            and other.op_token is None  # an operation's copy is settled by that operation
        ):
            other.state = FileState.UNKNOWN
            other.updated_at = now
            tr.bump(other)
    file.hash = hash.lower()
    file.size = size
    if copy.pending_metadata is not None:
        file.file_metadata = copy.pending_metadata
    if copy.pending_content_date is not None:
        file.content_date = copy.pending_content_date
    file.updated_by = user if user is not None else copy.pending_by
    file.updated_at = now
    _observe(copy, size, mtime, hash)
    tr.release(copy, FileState.AVAILABLE)
    tr.bump(copy)
    copy.published_token = published_token
    recompute_latest(session, lock_series_by_id(session, file.series_id))


def release_overwrite(
    session: Session,
    snap: CopySnapshot,
    *,
    size: int | None = None,
    mtime: datetime | None = None,
    hash: str | None = None,
) -> FileState:
    """End an overwrite that did not publish.

    With an observation of the original content (``hash`` equal to the file
    hash), the copy returns to ``available`` with it; otherwise it becomes
    ``unknown`` (the original could not be confirmed).

    Returns:
        The copy's new state.

    Raises:
        StaleOperation: the overwrite is no longer this operation's.
    """
    copy = _lock_owned(session, snap, frozenset({FileState.OVERWRITING}))
    file = session.get_one(File, copy.file_id)
    if hash is not None and size is not None and hash.lower() == file.hash:
        _observe(copy, size, mtime, hash)
        state = FileState.AVAILABLE
    else:
        if size is not None:
            _observe(copy, size, mtime, None)
        state = FileState.UNKNOWN
        tr.bump(copy)
    tr.release(copy, state)
    recompute_latest(session, lock_series_by_id(session, file.series_id))
    return state


def abandon(session: Session, snap: CopySnapshot) -> bool:
    """Give up an operation that could not finish, so the next repair retries it at once.

    A ``deleting`` copy loses its token (a delete that did not finish); any
    other owned copy keeps its token with the deadline set to now (expired).

    Returns:
        True if the copy was still this operation's.
    """
    try:
        copy = tr.lock_copy(session, snap.copy_id, token=snap.token, require_live=False)
    except (tr.StaleOperation, tr.CopyBusy):
        return False
    if copy.state == FileState.DELETING:
        tr.release(copy, FileState.DELETING)
    else:
        copy.op_deadline = tr.clock()
    return True


def find_expired(session: Session, storage_location_id: int, now: datetime) -> list[int]:
    """Return the ids of a storage location's copies that need :func:`repair_expired`.

    These are copies whose operation token expired at ``now``, and
    ``deleting`` copies without a token (a delete that did not finish).

    Args:
        session: an open session.
        storage_location_id: the storage location.
        now: the time expiry is judged at.
    """
    expired = (FileStorageLocation.op_token.is_not(None)) & (FileStorageLocation.op_deadline <= now)
    failed_delete = (FileStorageLocation.op_token.is_(None)) & (
        FileStorageLocation.state == FileState.DELETING
    )
    return list(
        session.scalars(
            select(FileStorageLocation.id)
            .where(
                FileStorageLocation.storage_location_id == storage_location_id,
                expired | failed_delete,
            )
            .order_by(FileStorageLocation.id)
        )
    )

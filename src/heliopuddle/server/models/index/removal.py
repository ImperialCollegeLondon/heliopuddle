"""Copies an index run found gone, and re-checking a storage location's observations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import ColumnElement, and_, delete, or_, select, update
from sqlalchemy.orm import Session

from heliopuddle.server.database.orm import (
    File,
    FileStorageLocation,
    IndexProgress,
    utcnow,
)
from heliopuddle.server.models import transitions
from heliopuddle.server.models.series import lock_series_by_id, recompute_latest
from heliopuddle.server.schemas import FileState

RemoveOutcome = Literal["removed", "missing", "skipped"]


@dataclass(frozen=True)
class RemovalCandidate:
    """A copy a full index did not see in its listing.

    Attributes:
        copy_id: the copy.
        generation: its generation when selected (the revision the change expects).
        path: the file's relative path.
    """

    copy_id: int
    generation: int
    path: str


def _removable() -> ColumnElement[bool]:
    """``available``/``unknown`` copies, and ``pending`` copies never observed (SVC-12)."""
    fsl = FileStorageLocation
    return or_(
        fsl.state.in_((FileState.AVAILABLE, FileState.UNKNOWN)),
        and_(fsl.state == FileState.PENDING, fsl.mtime.is_(None), fsl.last_seen_at.is_(None)),
    )


def removal_candidates(
    session: Session,
    storage_location_id: int,
    before: datetime,
    *,
    after_id: int = 0,
    limit: int = 1000,
) -> list[RemovalCandidate]:
    """Return one page of the copies a full index did not see (set-free, SVC-17).

    Every listed copy gets ``last_seen_at`` = the listing start, so the
    candidates are the storage location's copies of live files that no
    operation owns (no token), last seen before ``before`` (or never) and
    unchanged since ``before``: ``available`` and ``unknown`` copies, and
    ``pending`` copies never observed (left by a storage URL change). Pages
    are by copy id (keyset), so any number of copies streams through short
    read transactions.

    Args:
        session: an open session.
        storage_location_id: the storage location.
        before: the listing start time.
        after_id: only copies with a larger id (the last id of the previous page).
        limit: the page size.
    """
    fsl = FileStorageLocation
    rows = session.execute(
        select(fsl.id, fsl.generation, File.path)
        .join(File, File.id == fsl.file_id)
        .where(
            fsl.storage_location_id == storage_location_id,
            fsl.id > after_id,
            _removable(),
            fsl.op_token.is_(None),
            or_(fsl.last_seen_at.is_(None), fsl.last_seen_at < before),
            fsl.updated_at < before,
            File.deleted_at.is_(None),
        )
        .order_by(fsl.id)
        .limit(limit)
    ).all()
    return [RemovalCandidate(copy_id, generation, path) for copy_id, generation, path in rows]


def index_remove_copy(
    session: Session,
    copy_id: int,
    *,
    before: datetime,
    accept: bool,
    user: str | None,
    generation: int | None = None,
) -> RemoveOutcome:
    """Record that a copy is gone from its store (full index only).

    With ``accept`` the copy becomes ``deleted`` and, when the file has no
    other non-deleted copy, the file is soft-deleted (``updated_by`` =
    ``user``). Otherwise the copy becomes ``missing``. ``is_latest`` is
    recomputed under the series lock. The change is guarded
    (:func:`heliopuddle.server.models.transitions.lock_copy`): a copy that
    changed since it was selected (``generation``) or since ``before``, that
    has a token, that was seen at or after ``before``, or that is no longer a
    removal candidate, is left alone (``skipped``).

    Args:
        session: a session in a write transaction; the caller commits.
        copy_id: the copy.
        before: the listing start time.
        accept: the storage location accepts external changes.
        user: ``updated_by`` for a soft-deleted file.
        generation: the copy's generation when it was selected, if known.

    Returns:
        ``removed``, ``missing`` or ``skipped``.
    """
    try:
        copy = transitions.lock_copy(
            session,
            copy_id,
            generation=generation,
            states=(FileState.AVAILABLE, FileState.UNKNOWN, FileState.PENDING),
        )
    except (transitions.StaleOperation, transitions.CopyBusy):
        return "skipped"
    never_observed = copy.mtime is None and copy.last_seen_at is None
    if (
        copy.op_token is not None
        or (copy.state == FileState.PENDING and not never_observed)
        or copy.updated_at >= before
        or (copy.last_seen_at is not None and copy.last_seen_at >= before)
    ):
        return "skipped"
    file = session.get_one(File, copy.file_id)
    series = lock_series_by_id(session, file.series_id)
    now = utcnow()
    copy.state = FileState.DELETED if accept else FileState.MISSING
    copy.updated_at = now
    transitions.bump(copy)
    if accept and all(c.state == FileState.DELETED for c in file.copies):
        file.deleted_at = now
        file.updated_by = user
        file.updated_at = now
    recompute_latest(session, series)
    return "removed" if accept else "missing"


def stale_copies(
    session: Session, storage_location_id: int, state: FileState, older_than: datetime
) -> list[tuple[int, int]]:
    """Return ``(copy_id, file_id)`` of token-less ``state`` copies changed before ``older_than``.

    Copies with a token (live or expired) belong to an operation; expired
    ones are repaired through that operation's own repair, not here.
    """
    rows = session.execute(
        select(FileStorageLocation.id, FileStorageLocation.file_id)
        .join(File, File.id == FileStorageLocation.file_id)
        .where(
            FileStorageLocation.storage_location_id == storage_location_id,
            FileStorageLocation.state == state,
            FileStorageLocation.op_token.is_(None),
            FileStorageLocation.updated_at < older_than,
            File.deleted_at.is_(None),
        )
        .order_by(FileStorageLocation.id)
    ).all()
    return [(copy_id, file_id) for copy_id, file_id in rows]


def copy_awaits_repair(session: Session, copy_id: int, state: FileState) -> bool:
    """True if the copy is still token-less in ``state`` (a repair did not take effect)."""
    row = session.execute(
        select(FileStorageLocation.state, FileStorageLocation.op_token).where(
            FileStorageLocation.id == copy_id
        )
    ).one_or_none()
    return row is not None and row[0] == state and row[1] is None


def reset_observations(session: Session, storage_location_id: int) -> int:
    """Reset a storage location's copies after its URL changed (SVC-12).

    Every copy that is not ``deleted`` or ``deleting`` becomes ``pending``
    without a token (known content awaiting verification, latest-eligible)
    and keeps its hash and size. Observations (``mtime``, ``last_seen_at``),
    tokens and any staged overwrite (``pending_hash``, ``pending_size``) are
    cleared, so an ``overwriting`` copy returns to its old content and a
    ``missing`` one is looked for again. A ``deleting`` copy stays
    ``deleting`` without a token (the index retries the delete, which never
    removes an object hp has not observed in the new root). ``generation``
    goes up by one on all of them, so every operation started against the old
    root is stale. The storage location's index progress is deleted, so
    adoption runs a full index that re-checks each copy. ``is_latest`` is
    recomputed for every affected series.

    Args:
        session: a session in a write transaction; the caller commits.
        storage_location_id: the storage location.

    Returns:
        The number of copies reset.
    """
    fsl = FileStorageLocation
    affected = select(fsl.id).where(
        fsl.storage_location_id == storage_location_id, fsl.state != FileState.DELETED
    )
    series_ids = session.scalars(
        select(File.series_id)
        .join(fsl, fsl.file_id == File.id)
        .where(fsl.id.in_(affected), File.deleted_at.is_(None))
        .distinct()
        .order_by(File.series_id)
    ).all()
    for series_id in series_ids:  # every writer locks the series first, in id order
        lock_series_by_id(session, series_id)
    cleared = {
        "mtime": None,
        "last_seen_at": None,
        "op_token": None,
        "op_deadline": None,
        "pending_hash": None,
        "pending_size": None,
        "pending_metadata": None,
        "pending_content_date": None,
        "pending_by": None,
        "pending_staged": False,
        "published_token": None,
        "generation": fsl.generation + 1,
        "updated_at": utcnow(),
    }
    of_location = fsl.storage_location_id == storage_location_id
    reset = session.execute(
        update(fsl)
        .where(of_location, fsl.state.not_in((FileState.DELETED, FileState.DELETING)))
        .values(state=FileState.PENDING, **cleared),
        execution_options={"synchronize_session": False},
    )
    deleting = session.execute(
        update(fsl).where(of_location, fsl.state == FileState.DELETING).values(**cleared),
        execution_options={"synchronize_session": False},
    )
    session.execute(
        delete(IndexProgress).where(IndexProgress.storage_location_id == storage_location_id)
    )
    session.expire_all()
    for series_id in series_ids:
        recompute_latest(session, lock_series_by_id(session, series_id))
    return sum(int(getattr(r, "rowcount", 0) or 0) for r in (reset, deleting))

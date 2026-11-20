"""Record what an index run saw: add a file, set a copy's state, touch unchanged copies."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select, tuple_, update
from sqlalchemy.orm import Session

from heliopuddle.config.models import VersionRule
from heliopuddle.server.database.orm import (
    File,
    FileStorageLocation,
    utcnow,
)
from heliopuddle.server.models import transitions
from heliopuddle.server.models.files import register_file
from heliopuddle.server.models.series import lock_series_by_id, recompute_latest
from heliopuddle.server.schemas import FileState


def index_add_file(
    session: Session,
    *,
    project: str,
    storage_location: str,
    path: str,
    hash: str,
    size: int,
    mtime: datetime | None,
    state: FileState,
    rules: Sequence[VersionRule],
    user: str | None,
    seen_at: datetime,
) -> int:
    """Record a file found in a store with no live file at its path.

    Goes through :func:`register_file` (series, version and ``content_date``
    from the version rules; ``created_by`` = ``user``), then sets the copy to
    ``state`` with what was seen, and recomputes ``is_latest``.

    Args:
        session: a session in a write transaction; the caller commits.
        project: project name.
        storage_location: storage location name.
        path: relative path.
        hash: md5 of the content.
        size: size in the store.
        mtime: mtime in the store.
        state: ``available`` (external changes accepted) or ``unknown``.
        rules: the project's version rules.
        user: ``created_by``.
        seen_at: when the file was listed.

    Returns:
        The new file id.
    """
    result = register_file(
        session,
        project=project,
        storage_location=storage_location,
        path=path,
        hash=hash,
        size=size,
        created_by=user,
        rules=rules,
    )
    copy = session.get_one(FileStorageLocation, result.copy_id)
    _observe(copy, state, size, mtime, hash if state == FileState.AVAILABLE else None, seen_at)
    file = session.get_one(File, result.file_id)
    recompute_latest(session, lock_series_by_id(session, file.series_id))
    return result.file_id


def index_set_copy(
    session: Session,
    *,
    file_id: int,
    storage_location_id: int,
    state: FileState,
    size: int,
    mtime: datetime | None,
    hash: str | None,
    seen_at: datetime,
    new_file_hash: bool = False,
    user: str | None = None,
) -> FileStorageLocation:
    """Record what the index saw for a live file's copy, creating the copy if needed.

    The caller holds the series lock (:func:`lock_index_target`). With
    ``new_file_hash`` the file takes ``hash`` and ``size`` as its accepted
    content (``updated_by`` = ``user``); if that changes the hash, its other
    ``pending``/``available``/``missing`` copies become ``unknown``, since
    they hold the old content. Every copy whose state or hash changes gets a
    new ``generation``. ``is_latest`` is recomputed.

    Args:
        session: a session in a write transaction; the caller commits.
        file_id: the live file.
        storage_location_id: the storage location.
        state: the copy's new state.
        size: size in the store.
        mtime: mtime in the store.
        hash: md5 in the store, if computed.
        seen_at: when it was listed.
        new_file_hash: accept the store's content as the file's content.
        user: ``updated_by`` when the file changes.

    Returns:
        The copy.
    """
    file = session.get_one(File, file_id)
    copy = session.scalar(
        select(FileStorageLocation).where(
            FileStorageLocation.file_id == file_id,
            FileStorageLocation.storage_location_id == storage_location_id,
        )
    )
    if copy is None:
        copy = FileStorageLocation(file_id=file_id, storage_location_id=storage_location_id)
        session.add(copy)
    _observe(copy, state, size, mtime, hash, seen_at)
    if new_file_hash and hash is not None:
        now = utcnow()
        if file.hash != hash.lower():
            for other in file.copies:
                if (
                    other is not copy
                    and other.state in _OLD_CONTENT_STATES
                    and other.op_token is None  # an operation's copy is left to it
                ):
                    other.state = FileState.UNKNOWN
                    other.updated_at = now
                    transitions.bump(other)
        file.hash = hash.lower()
        file.size = size
        file.updated_by = user
        file.updated_at = now
    recompute_latest(session, lock_series_by_id(session, file.series_id))
    return copy


_OLD_CONTENT_STATES = (FileState.PENDING, FileState.AVAILABLE, FileState.MISSING)
"""Copies that become ``unknown`` when their file accepts different content."""


def _observe(
    copy: FileStorageLocation,
    state: FileState,
    size: int,
    mtime: datetime | None,
    hash: str | None,
    seen_at: datetime,
) -> None:
    new_hash = hash.lower() if hash else None
    if copy.id is None or copy.state != state or copy.hash != new_hash:
        transitions.bump(copy)  # a content-changing transition
    copy.state = state
    copy.size = size
    copy.mtime = mtime
    copy.hash = new_hash
    copy.last_seen_at = seen_at
    copy.updated_at = utcnow()


_TOUCHED_STATES = (FileState.AVAILABLE, FileState.UNKNOWN)


def touch_copies(session: Session, copies: Sequence[tuple[int, int]], seen_at: datetime) -> None:
    """Set ``last_seen_at`` on copies found unchanged (no state change, so no series lock).

    One statement for all ``(copy_id, generation)`` pairs. A copy whose
    generation changed meanwhile is left alone, as is any copy that is not
    ``available`` or ``unknown`` (a ``pending`` copy stays "never observed"
    until it is re-checked).
    """
    if copies:
        session.execute(
            update(FileStorageLocation)
            .where(
                tuple_(FileStorageLocation.id, FileStorageLocation.generation).in_(
                    [(copy_id, generation) for copy_id, generation in copies]
                ),
                FileStorageLocation.state.in_(_TOUCHED_STATES),
            )
            .values(last_seen_at=seen_at, updated_at=FileStorageLocation.updated_at),
            execution_options={"synchronize_session": False},
        )

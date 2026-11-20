"""Index progress, checkpoints and the incremental candidate stream (SVC-08)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from heliopuddle.server.database.orm import (
    File,
    FileStorageLocation,
    IndexProgress,
    Project,
    StorageLocation,
)
from heliopuddle.server.models.rules import change_key_page, change_time, through_time
from heliopuddle.server.schemas.rules import RuleCursor


def get_index_progress(
    session: Session, project: str, storage_location: str
) -> IndexProgress | None:
    """Return the index watermarks of a storage location, or None if never indexed."""
    return session.scalar(
        select(IndexProgress)
        .join(StorageLocation, StorageLocation.id == IndexProgress.storage_location_id)
        .join(Project, Project.id == StorageLocation.project_id)
        .where(Project.name == project, StorageLocation.name == storage_location)
    )


def record_index_progress(
    session: Session,
    storage_location_id: int,
    mode: Literal["full", "incremental"],
    at: datetime,
    *,
    expected_url: str | None = None,
) -> IndexProgress:
    """Upsert ``last_full_at`` or ``last_incremental_at`` for a storage location.

    The index flows call this only on success, holding the storage location's
    concurrency limit, so two writers never race on the insert.
    """
    _check_progress_storage(session, storage_location_id, expected_url)
    progress = session.get(IndexProgress, storage_location_id, with_for_update=True)
    if progress is None:
        progress = IndexProgress(storage_location_id=storage_location_id)
        session.add(progress)
    if mode == "full":
        progress.last_full_at = at
        progress.full_started_at = None
        progress.full_cursor_path = None
    else:
        progress.last_incremental_at = at
        progress.incremental_started_at = None
        progress.incremental_since = None
        progress.incremental_cursor_path = None
        progress.incremental_cursor_at = None
        progress.incremental_cursor_id = None
    session.flush()
    return progress


def _check_progress_storage(
    session: Session, storage_location_id: int, expected_url: str | None
) -> None:
    if expected_url is None:
        return
    storage = session.get(StorageLocation, storage_location_id, with_for_update={"key_share": True})
    if storage is None or not storage.active or storage.url.rstrip("/") != expected_url.rstrip("/"):
        raise RuntimeError("storage location changed while indexing; progress was not advanced")


@dataclass
class IndexCheckpoint:
    """A bounded chain's original selection window and its completed-work cursors."""

    started_at: datetime
    since: datetime | None = None
    path: str | None = None
    cursor: RuleCursor | None = None


def index_checkpoint(
    session: Session, storage_location_id: int, mode: Literal["full", "incremental"]
) -> IndexCheckpoint | None:
    """Read an unfinished chain, including runs paused with retrigger disabled."""
    row = session.get(IndexProgress, storage_location_id)
    if row is None:
        return None
    if mode == "full":
        return (
            IndexCheckpoint(row.full_started_at, path=row.full_cursor_path)
            if row.full_started_at is not None
            else None
        )
    if row.incremental_started_at is None:
        return None
    cursor = (
        RuleCursor(updated_at=row.incremental_cursor_at, id=row.incremental_cursor_id)
        if row.incremental_cursor_at is not None and row.incremental_cursor_id is not None
        else None
    )
    return IndexCheckpoint(
        row.incremental_started_at, row.incremental_since, row.incremental_cursor_path, cursor
    )


def save_index_checkpoint(
    session: Session,
    storage_location_id: int,
    mode: Literal["full", "incremental"],
    checkpoint: IndexCheckpoint,
    *,
    expected_url: str | None = None,
) -> None:
    """Persist only completed work, leaving completed-chain watermarks unchanged."""
    # Config URL resets take this same storage-row lock before deleting progress.
    # It also fences the first checkpoint insert, where no progress row exists.
    _check_progress_storage(session, storage_location_id, expected_url)
    row = session.get(IndexProgress, storage_location_id, with_for_update=True)
    if row is None:
        row = IndexProgress(storage_location_id=storage_location_id)
        session.add(row)
    if mode == "full":
        row.full_started_at = checkpoint.started_at
        row.full_cursor_path = checkpoint.path
    else:
        row.incremental_started_at = checkpoint.started_at
        row.incremental_since = checkpoint.since
        row.incremental_cursor_path = checkpoint.path
        row.incremental_cursor_at = checkpoint.cursor.updated_at if checkpoint.cursor else None
        row.incremental_cursor_id = checkpoint.cursor.id if checkpoint.cursor else None


@dataclass(frozen=True)
class IndexChange:
    """A live source copy whose DB change key belongs to an incremental window."""

    file_id: int
    path: str
    cursor: RuleCursor


def incremental_candidates(
    session: Session,
    storage_location_id: int,
    *,
    since: datetime | None,
    through: datetime,
    after: RuleCursor | None = None,
    prefixes: Sequence[str] = ("",),
    limit: int = 1000,
) -> list[IndexChange]:
    """Page changed files/copies with an inclusive window and a strict two-part cursor.

    The upper bound excludes this chain's own observations. Unindexed and
    newer store objects are found separately by the streaming path walk.
    """
    base = (
        select(File.id, File.path, change_time().label("changed_at"))
        .join(FileStorageLocation, FileStorageLocation.file_id == File.id)
        .where(
            FileStorageLocation.storage_location_id == storage_location_id,
            File.deleted_at.is_(None),
            through_time(through),
            or_(
                *(
                    File.path.startswith(prefix + "/", autoescape=True) if prefix else File.id > 0
                    for prefix in prefixes
                )
            ),
        )
    )
    # ``change_time() >= since`` is the strict key ``> (since, 0)``: file ids start at 1.
    lower = after
    if since is not None and (lower is None or (lower.updated_at, lower.id) < (since, 0)):
        lower = RuleCursor(updated_at=since, id=0)
    size = limit
    while True:
        rows, exhausted = change_key_page(session, base, lower=lower, size=size)
        if rows or exhausted:
            break
        size *= 2
    return [
        IndexChange(row[0], row[1], RuleCursor(updated_at=row.changed_at, id=row[0]))
        for row in rows[:limit]
    ]

"""A file's copies in storage locations, and the file's API schema."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from heliopuddle.server.database.orm import File, FileStorageLocation, Series, utcnow
from heliopuddle.server.models.files.errors import NotFoundError
from heliopuddle.server.models.series import lock_series_of_file, recompute_latest
from heliopuddle.server.schemas import FileRead, FileState, FileStorageLocationRead
from heliopuddle.versioning import display_series_key


def get_copy(session: Session, file_storage_location_id: int) -> FileStorageLocation | None:
    """Return a copy with its file, project and storage location loaded, or None."""
    return session.scalar(
        select(FileStorageLocation)
        .where(FileStorageLocation.id == file_storage_location_id)
        .options(
            joinedload(FileStorageLocation.file).joinedload(File.project),
            joinedload(FileStorageLocation.storage_location),
        )
    )


def _require_copy(
    session: Session, file_storage_location_id: int
) -> tuple[FileStorageLocation, Series | None]:
    """Lock the copy's series row, then the copy (series first, as every writer does)."""
    file_id = session.scalar(
        select(FileStorageLocation.file_id).where(
            FileStorageLocation.id == file_storage_location_id
        )
    )
    series = lock_series_of_file(session, file_id) if file_id is not None else None
    copy = session.get(FileStorageLocation, file_storage_location_id, with_for_update=True)
    if copy is None:
        raise NotFoundError(f"file storage location {file_storage_location_id} not found")
    return copy, series


def set_copy_verified(
    session: Session,
    file_storage_location_id: int,
    *,
    size: int,
    mtime: datetime | None,
    hash: str,
    seen_at: datetime | None = None,
) -> FileStorageLocation:
    """Mark a copy ``available`` and record what was seen in the store.

    Locks the series row and recomputes ``is_latest``, since the copy may make
    its file eligible to be latest (API-12).

    Args:
        session: a session in a write transaction; the caller commits.
        file_storage_location_id: the copy.
        size: size in the store.
        mtime: modification time in the store.
        hash: verified hex digest.
        seen_at: when it was seen; defaults to now.

    Raises:
        NotFoundError: if the copy does not exist.
    """
    copy, series = _require_copy(session, file_storage_location_id)
    copy.state = FileState.AVAILABLE
    copy.size = size
    copy.mtime = mtime
    copy.hash = hash.lower()
    copy.last_seen_at = seen_at or utcnow()
    copy.updated_at = utcnow()
    if series is not None:
        recompute_latest(session, series)
    return copy


def set_copy_state(
    session: Session, file_storage_location_id: int, state: FileState
) -> FileStorageLocation:
    """Set a copy's state.

    Locks the series row and recomputes ``is_latest``, since the state decides
    whether the file is eligible to be latest (API-12).

    Raises:
        NotFoundError: if the copy does not exist.
    """
    copy, series = _require_copy(session, file_storage_location_id)
    copy.state = state
    copy.updated_at = utcnow()
    if series is not None:
        recompute_latest(session, series)
    return copy


def _copy_read(copy: FileStorageLocation) -> FileStorageLocationRead:
    return FileStorageLocationRead(
        storage_location=copy.storage_location.name,
        state=copy.state,
        size=copy.size,
        mtime=copy.mtime,
        hash=copy.hash,
        last_seen_at=copy.last_seen_at,
        arrived_at=copy.arrived_at,
        updated_at=copy.updated_at,
    )


def to_file_read(file: File) -> FileRead:
    """Convert a file (with project, series and copies loaded) to its API schema."""
    copies = sorted(file.copies, key=lambda c: c.storage_location_id)
    return FileRead(
        id=file.id,
        project=file.project.name,
        path=file.path,
        name=file.name,
        series_id=file.series_id,
        version_number=file.version_number,
        version_text=file.version_text,
        content_date=file.content_date,
        size=file.size,
        hash=file.hash,
        hash_algo=file.hash_algo,
        metadata=file.file_metadata,
        is_latest=file.is_latest,
        ignored_from_all_rules=file.ignored_from_all_rules,
        created_at=file.created_at,
        updated_at=file.updated_at,
        deleted_at=file.deleted_at,
        created_by=file.created_by,
        updated_by=file.updated_by,
        series_key=display_series_key(file.series.series_key),
        storage_locations=[_copy_read(c) for c in copies],
    )

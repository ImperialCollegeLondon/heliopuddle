"""Accept an ``unknown`` copy as the file's content (API-11)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from heliopuddle.server.database.orm import (
    File,
    FileStorageLocation,
    Project,
    StorageLocation,
    utcnow,
)
from heliopuddle.server.models.files import NotFoundError
from heliopuddle.server.models.index.copies import index_set_copy
from heliopuddle.server.models.series import lock_series_by_id
from heliopuddle.server.schemas import FileState


class NotUnknownError(Exception):
    """The copy to accept is not in state ``unknown`` (or there is no copy there)."""


class AmbiguousCopyError(ValueError):
    """No storage location was given and the file has no, or several, ``unknown`` copies."""


@dataclass(frozen=True)
class AcceptTarget:
    """The ``unknown`` copy chosen by :func:`get_accept_target`."""

    copy_id: int
    file_id: int
    project: str
    storage_location: str
    path: str


def get_accept_target(
    session: Session, file_id: int, storage_location: str | None = None
) -> AcceptTarget:
    """Choose the copy to accept (D4).

    Args:
        session: an open session.
        file_id: a live file.
        storage_location: the storage location; None means the file's only
            ``unknown`` copy.

    Raises:
        NotFoundError: the file does not exist or is deleted, or the project
            has no storage location of that name.
        AmbiguousCopyError: no storage location was given and the file has no
            ``unknown`` copy, or several.
        NotUnknownError: the copy in that storage location is not ``unknown``
            (or there is none).
    """
    file = session.get(File, file_id)
    if file is None or file.deleted_at is not None:
        raise NotFoundError(f"file {file_id} not found")
    project = session.get_one(Project, file.project_id).name
    copies = {c.storage_location.name: c for c in file.copies}
    if storage_location is None:
        unknown = [name for name, c in copies.items() if c.state == FileState.UNKNOWN]
        if len(unknown) != 1:
            raise AmbiguousCopyError(
                f"file {file_id} has {len(unknown)} unknown copies; name the storage location"
            )
        storage_location = unknown[0]
    else:
        exists = session.scalar(
            select(StorageLocation.id).where(
                StorageLocation.project_id == file.project_id,
                StorageLocation.name == storage_location,
            )
        )
        if exists is None:
            raise NotFoundError(f"storage location {storage_location!r} not found in {project!r}")
    copy = copies.get(storage_location)
    if copy is None or copy.state != FileState.UNKNOWN:
        state = copy.state.value if copy is not None else "absent"
        raise NotUnknownError(
            f"file {file_id} copy in {storage_location!r} is {state}, not unknown"
        )
    return AcceptTarget(copy.id, file_id, project, storage_location, file.path)


def accept_copy_record(
    session: Session,
    target: AcceptTarget,
    *,
    size: int,
    mtime: datetime | None,
    hash: str,
    user: str | None,
) -> File:
    """Accept an ``unknown`` copy as it is now (the write step of D4).

    Under the series lock: the file takes the copy's hash and size
    (``updated_by`` = ``user``), the copy becomes ``available`` with what was
    seen, and ``is_latest`` is recomputed. If the hash changed, the file's
    other copies hold the old content, so they become ``unknown``.

    Raises:
        NotFoundError: the file was deleted meanwhile.
        NotUnknownError: the copy is no longer ``unknown``.
    """
    series_id = session.scalar(select(File.series_id).where(File.id == target.file_id))
    if series_id is None:  # pragma: no cover - files are never hard-deleted
        raise NotFoundError(f"file {target.file_id} not found")
    lock_series_by_id(session, series_id)
    file = session.get_one(File, target.file_id, populate_existing=True)
    if file.deleted_at is not None:
        raise NotFoundError(f"file {target.file_id} not found")
    copy = session.get_one(FileStorageLocation, target.copy_id, populate_existing=True)
    if copy.state != FileState.UNKNOWN:
        raise NotUnknownError(f"file {target.file_id} copy is {copy.state.value}, not unknown")
    index_set_copy(
        session,
        file_id=file.id,
        storage_location_id=copy.storage_location_id,
        state=FileState.AVAILABLE,
        size=size,
        mtime=mtime,
        hash=hash,
        seen_at=utcnow(),
        new_file_hash=True,
        user=user,
    )
    return file


def available_copy_ids(
    session: Session, storage_location_id: int, *, after_id: int = 0, limit: int = 100
) -> list[int]:
    """Return up to ``limit`` ids of unowned ``available`` copies, keyset-paged by id.

    Used by reconcile's verify phase; pass the last returned id as ``after_id``.
    """
    return list(
        session.scalars(
            select(FileStorageLocation.id)
            .where(
                FileStorageLocation.storage_location_id == storage_location_id,
                FileStorageLocation.state == FileState.AVAILABLE,
                FileStorageLocation.op_token.is_(None),
                FileStorageLocation.id > after_id,
            )
            .order_by(FileStorageLocation.id)
            .limit(limit)
        )
    )

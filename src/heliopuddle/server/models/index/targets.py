"""What the DB holds for a path in a storage location, and the check before a change."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from heliopuddle.server.database.orm import (
    File,
    FileStorageLocation,
)
from heliopuddle.server.models import transitions
from heliopuddle.server.models.series import lock_series_by_id
from heliopuddle.server.schemas import FileState


@dataclass(frozen=True)
class IndexTarget:
    """What the DB holds for a path in one storage location.

    Attributes:
        file_id: the live file at the path.
        series_id: its series.
        file_hash: its accepted hash.
        copy_id: its copy in the storage location (None if it has none).
        copy_state: the copy's state.
        copy_size: size last seen in the store.
        copy_mtime: mtime last seen in the store.
        file_size: the file's accepted size.
        copy_hash: hash last verified in the store.
        generation: the copy's generation (the revision a change expects).
        op_token: the token of the operation owning the copy, if any.
        op_deadline: when that ownership expires.
        last_seen_at: when an index or verify last saw the copy.
    """

    file_id: int
    series_id: int
    file_hash: str
    copy_id: int | None = None
    copy_state: FileState | None = None
    copy_size: int | None = None
    copy_mtime: datetime | None = None
    file_size: int = 0
    copy_hash: str | None = None
    generation: int | None = None
    op_token: str | None = None
    op_deadline: datetime | None = None
    last_seen_at: datetime | None = None

    def is_owned(self, now: datetime) -> bool:
        """True if a live (unexpired) operation owns the copy.

        The plain-row form of ``transitions.is_owned``, for skipping early;
        the guarded write re-checks under the lock.
        """
        return self.op_token is not None and self.op_deadline is not None and self.op_deadline > now

    @property
    def awaits_recheck(self) -> bool:
        """True for a token-less ``pending`` copy never observed in the current root.

        That is what a storage URL change leaves behind (SVC-12): the index
        re-hashes such a copy and compares it with the kept hash and size.
        """
        return (
            self.copy_state == FileState.PENDING
            and self.op_token is None
            and self.copy_mtime is None
            and self.last_seen_at is None
        )


def get_index_targets(
    session: Session, project_id: int, storage_location_id: int, paths: Sequence[str]
) -> dict[str, IndexTarget]:
    """Return the live files at ``paths`` with their copy in the storage location, by path.

    Paths without a live file are absent from the result.
    """
    if not paths:
        return {}
    rows = session.execute(
        select(
            File.path,
            File.id,
            File.series_id,
            File.hash,
            FileStorageLocation.id,
            FileStorageLocation.state,
            FileStorageLocation.size,
            FileStorageLocation.mtime,
            File.size,
            FileStorageLocation.hash,
            FileStorageLocation.generation,
            FileStorageLocation.op_token,
            FileStorageLocation.op_deadline,
            FileStorageLocation.last_seen_at,
        )
        .outerjoin(
            FileStorageLocation,
            (FileStorageLocation.file_id == File.id)
            & (FileStorageLocation.storage_location_id == storage_location_id),
        )
        .where(
            File.project_id == project_id,
            File.deleted_at.is_(None),
            File.path.in_(list(paths)),
        )
    ).all()
    return {row[0]: IndexTarget(*row[1:]) for row in rows}


def get_index_target(
    session: Session, project_id: int, storage_location_id: int, path: str
) -> IndexTarget | None:
    """Return :func:`get_index_targets` for one path, or None if there is no live file."""
    return get_index_targets(session, project_id, storage_location_id, [path]).get(path)


def lock_index_target(
    session: Session, project_id: int, storage_location_id: int, path: str, expected: IndexTarget
) -> bool:
    """Lock ``expected``'s series and check the DB still holds ``expected`` for ``path``.

    A target with a copy goes through the guarded-transition helper
    (:func:`heliopuddle.server.models.transitions.lock_copy`) with the copy's
    generation and state as read: a copy that changed, or that another live
    operation owns, is left alone. Then the whole target (file and copy) is
    compared with what was read.

    Returns:
        True if nothing changed since ``expected`` was read (the caller may
        apply its change), else False (the file is left for the next run).
    """
    if expected.copy_id is None or expected.copy_state is None:
        lock_series_by_id(session, expected.series_id)
    else:
        try:
            transitions.lock_copy(
                session,
                expected.copy_id,
                generation=expected.generation,
                states={expected.copy_state},
            )
        except (transitions.StaleOperation, transitions.CopyBusy):
            return False
    return get_index_target(session, project_id, storage_location_id, path) == expected

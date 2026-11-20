"""Snapshots of copies: what an operation read before acting on a store.

An operation reads a :class:`CopySnapshot`, does its store I/O outside any
transaction, then applies the result through a guarded transition
(:func:`heliopuddle.server.models.transitions.lock_copy`) that checks the
snapshot's token and generation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from heliopuddle.server.database.orm import File, FileStorageLocation, Project, StorageLocation
from heliopuddle.server.schemas import FileState


@dataclass(frozen=True)
class CopySnapshot:
    """What an operation read about a copy, to act on the store and apply the result.

    Attributes:
        copy_id: the copy.
        file_id: its file.
        project: project name.
        storage_location: storage location name.
        storage_location_id: storage location id.
        path: the file's path.
        state: the copy state when read.
        token: the operation token now owning the copy (None if it has none).
        deadline: when that ownership expires.
        generation: the copy generation when read.
        file_hash: the file's accepted hash.
        file_size: the file's accepted size.
        pending_hash: the content an upload intends to write.
        pending_size: its size.
        size: size last observed in the store.
        mtime: mtime last observed in the store.
        hash: hash last verified in the store.
        last_seen_at: when it was last observed.
        staged: the pending upload writes a staging path and publishes it.
        move_destination_id: for a move's source delete, the verified destination
            copy a retried delete must re-check first.
    """

    copy_id: int
    file_id: int
    project: str
    storage_location: str
    storage_location_id: int
    path: str
    state: FileState
    token: str | None
    deadline: datetime | None
    generation: int
    file_hash: str
    file_size: int
    pending_hash: str | None
    pending_size: int | None
    size: int | None
    mtime: datetime | None
    hash: str | None
    last_seen_at: datetime | None
    staged: bool = False
    move_destination_id: int | None = None

    @property
    def created_by_reservation(self) -> bool:
        """True for a token-holding ``pending`` copy whose row the reservation created."""
        return self.state == FileState.PENDING and self.generation == 0

    @property
    def observed(self) -> bool:
        """True if hp has seen this copy's object (size and time of an observation)."""
        return self.size is not None and self.last_seen_at is not None


def copy_snapshot(session: Session, copy: FileStorageLocation) -> CopySnapshot:
    """Return a snapshot of a loaded copy."""
    file = session.get_one(File, copy.file_id)
    location = session.get_one(StorageLocation, copy.storage_location_id)
    project = session.scalars(select(Project.name).where(Project.id == file.project_id)).one()
    return CopySnapshot(
        copy_id=copy.id,
        file_id=file.id,
        project=project,
        storage_location=location.name,
        storage_location_id=location.id,
        path=file.path,
        state=copy.state,
        token=copy.op_token,
        deadline=copy.op_deadline,
        generation=copy.generation,
        file_hash=file.hash,
        file_size=file.size,
        pending_hash=copy.pending_hash,
        pending_size=copy.pending_size,
        size=copy.size,
        mtime=copy.mtime,
        hash=copy.hash,
        last_seen_at=copy.last_seen_at,
        staged=bool(copy.pending_staged),
        move_destination_id=copy.move_destination_id,
    )


def snapshot(session: Session, copy_id: int) -> CopySnapshot | None:
    """Return a snapshot of a copy, or None if it does not exist."""
    copy = session.get(FileStorageLocation, copy_id)
    return copy_snapshot(session, copy) if copy is not None else None


__all__ = ["CopySnapshot", "copy_snapshot", "snapshot"]

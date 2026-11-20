"""Short, guarded DB phases for transfers and source-only deletes (SVC-19/20).

Transfers use pending upload ownership, including staged publication when an
object already exists. They never change the logical file hash: the existing
upload repair can therefore settle them without reviving stale source content.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from heliopuddle.server.database.orm import File, FileStorageLocation, StorageLocation, utcnow
from heliopuddle.server.models import transitions as tr
from heliopuddle.server.models import uploads
from heliopuddle.server.models.files import InactiveError, NotFoundError
from heliopuddle.server.models.series import lock_series_by_id, recompute_latest
from heliopuddle.server.models.snapshots import CopySnapshot, copy_snapshot
from heliopuddle.server.schemas import FileState


@dataclass(frozen=True)
class TransferSnapshot:
    """Source revision and destination identity captured before store I/O."""

    source: CopySnapshot
    source_url: str
    destination: str
    destination_id: int
    destination_url: str
    destination_copy: CopySnapshot | None


def _recompute(session: Session, file_id: int) -> None:
    series_id = session.get_one(File, file_id).series_id
    recompute_latest(session, lock_series_by_id(session, series_id))


def _location(session: Session, project_id: int, name: str) -> StorageLocation:
    location = session.scalar(
        select(StorageLocation).where(
            StorageLocation.project_id == project_id, StorageLocation.name == name
        )
    )
    if location is None:
        raise NotFoundError(f"storage location {name!r} not found")
    if not location.active:
        raise InactiveError(f"storage location {name!r} is not active")
    return location


def source_snapshot(
    session: Session,
    file_id: int,
    source: str,
    *,
    expected_generation: int | None = None,
    expected_hash: str | None = None,
) -> tuple[CopySnapshot, str]:
    """Read an available source without owning it or changing any DB state.

    Args:
        session: an open read or write session.
        file_id: the logical file.
        source: source storage location name.
        expected_generation: require this revision, when deleting after a move.
        expected_hash: require this accepted content hash, when supplied.
    """
    file = session.get(File, file_id)
    if file is None or file.deleted_at is not None:
        raise NotFoundError(f"file {file_id} not found")
    location = _location(session, file.project_id, source)
    copy = session.scalar(
        select(FileStorageLocation).where(
            FileStorageLocation.file_id == file_id,
            FileStorageLocation.storage_location_id == location.id,
        )
    )
    if copy is None:
        raise NotFoundError(f"file {file_id} has no copy in {source!r}")
    tr.check_free(copy, tr.clock())
    if (
        copy.state != FileState.AVAILABLE
        or copy.hash != file.hash
        or copy.size != file.size
        or (expected_generation is not None and copy.generation != expected_generation)
        or (expected_hash is not None and file.hash != expected_hash.lower())
    ):
        raise tr.StaleOperation(f"source copy {copy.id} is not the expected available revision")
    return copy_snapshot(session, copy), location.url


def transfer_snapshot(
    session: Session, file_id: int, source: str, destination: str
) -> TransferSnapshot:
    """Read both endpoints; a live owner or unfinished operation is never ignored."""
    if source == destination:
        raise ValueError("source and destination storage locations must differ")
    snap, source_url = source_snapshot(session, file_id, source)
    file = session.get_one(File, file_id)
    location = _location(session, file.project_id, destination)
    copy = session.scalar(
        select(FileStorageLocation).where(
            FileStorageLocation.file_id == file_id,
            FileStorageLocation.storage_location_id == location.id,
        )
    )
    if copy is not None:
        tr.check_free(copy, tr.clock())
    return TransferSnapshot(
        snap,
        source_url,
        destination,
        location.id,
        location.url,
        copy_snapshot(session, copy) if copy is not None else None,
    )


def indexed_size(session: Session, storage_location_id: int) -> int:
    """Return indexed bytes in non-deleted copies, including unknown and missing copies."""
    return int(
        session.scalar(
            select(func.coalesce(func.sum(FileStorageLocation.size), 0)).where(
                FileStorageLocation.storage_location_id == storage_location_id,
                FileStorageLocation.state != FileState.DELETED,
            )
        )
        or 0
    )


def guard_storage_location(session: Session, storage_location_id: int, url: str) -> None:
    """Refuse an inactive endpoint or a URL changed since the operation started."""
    location = session.get(StorageLocation, storage_location_id, populate_existing=True)
    if location is None or not location.active or location.url != url:
        raise tr.StaleOperation(f"storage location {storage_location_id} changed")


def guard_source(session: Session, snap: CopySnapshot, url: str) -> FileStorageLocation:
    """Recheck a source revision under its series lock, without reserving it."""
    copy = tr.lock_copy(
        session,
        snap.copy_id,
        generation=snap.generation,
        expected_hash=snap.file_hash,
        states={FileState.AVAILABLE},
    )
    guard_storage_location(session, snap.storage_location_id, url)
    if (
        copy.hash != snap.file_hash
        or copy.size != snap.file_size
        or copy.file.size != snap.file_size
    ):
        raise tr.StaleOperation(f"source copy {snap.copy_id} changed")
    return copy


def _destination(session: Session, plan: TransferSnapshot) -> FileStorageLocation:
    guard_source(session, plan.source, plan.source_url)
    guard_storage_location(session, plan.destination_id, plan.destination_url)
    copy = session.scalar(
        select(FileStorageLocation)
        .where(
            FileStorageLocation.file_id == plan.source.file_id,
            FileStorageLocation.storage_location_id == plan.destination_id,
        )
        .with_for_update()
    )
    previous = plan.destination_copy
    if copy is None:
        if previous is not None:
            raise tr.StaleOperation("the destination copy was removed")
        copy = FileStorageLocation(
            file_id=plan.source.file_id,
            storage_location_id=plan.destination_id,
            state=FileState.PENDING,
            generation=0,
        )
        session.add(copy)
        session.flush()
        return copy
    tr.check_free(copy, tr.clock())
    if previous is None or copy.id != previous.copy_id:
        raise tr.StaleOperation("another operation created the destination copy")
    return tr.lock_copy(
        session,
        copy.id,
        generation=previous.generation,
        expected_hash=plan.source.file_hash,
    )


def reserve_destination(
    session: Session,
    plan: TransferSnapshot,
    *,
    staged: bool,
    lease: timedelta,
    user: str | None = None,
) -> CopySnapshot:
    """Reserve only the destination, using ownership understood by upload repair."""
    copy = _destination(session, plan)
    if plan.destination_copy is not None:
        tr.bump(copy)
    tr.take(copy, FileState.PENDING, uploads.new_token(), tr.clock() + lease)
    copy.pending_hash = plan.source.file_hash
    copy.pending_size = plan.source.file_size
    copy.pending_staged = staged
    copy.pending_by = user
    session.flush()
    _recompute(session, copy.file_id)
    return copy_snapshot(session, copy)


def guard_destination(
    session: Session, plan: TransferSnapshot, snap: CopySnapshot
) -> FileStorageLocation:
    """Check that the transfer still owns the same destination and accepted hash."""
    copy = tr.lock_copy(
        session,
        snap.copy_id,
        token=snap.token,
        generation=snap.generation,
        states={FileState.PENDING},
        expected_hash=plan.source.file_hash,
    )
    guard_storage_location(session, plan.destination_id, plan.destination_url)
    if copy.file.size != plan.source.file_size:
        raise tr.StaleOperation("the accepted file size changed")
    return copy


def stage_destination(session: Session, plan: TransferSnapshot, snap: CopySnapshot) -> CopySnapshot:
    """Protect an object discovered after reservation by switching to a staged write."""
    copy = guard_destination(session, plan, snap)
    copy.pending_staged = True
    return replace(snap, staged=True)


def mark_arrived(session: Session, plan: TransferSnapshot, snap: CopySnapshot) -> CopySnapshot:
    """Restart a move's destination arrival time when its content was already there (SVC-29).

    Nothing was written to the destination, but the file now lives there
    because of the move, so its cooling-off period starts again. The copy is
    locked under its series and checked against ``snap``; the generation is
    bumped so any rule candidate selected before the reset (e.g. a scheduled
    delete) is stale and re-checked against the new arrival time.

    Args:
        session: a write session.
        plan: the move.
        snap: the verified, available destination copy.

    Returns:
        The refreshed destination snapshot; the move's source delete must use it.

    Raises:
        StaleOperation: if the destination changed since ``snap``.
    """
    copy = tr.lock_copy(
        session,
        snap.copy_id,
        generation=snap.generation,
        states={FileState.AVAILABLE},
        expected_hash=plan.source.file_hash,
    )
    tr.check_free(copy, tr.clock())
    guard_storage_location(session, plan.destination_id, plan.destination_url)
    copy.arrived_at = utcnow()
    tr.bump(copy)
    session.flush()
    return copy_snapshot(session, copy)


def record_destination(
    session: Session,
    plan: TransferSnapshot,
    *,
    size: int,
    mtime: datetime | None,
    hash: str,
    available: bool,
) -> CopySnapshot:
    """Record a read-only destination decision, guarded against intervening owners."""
    copy = _destination(session, plan)
    if available and (hash != plan.source.file_hash or size != plan.source.file_size):
        raise tr.StaleOperation("destination content differs from the source snapshot")
    if available and copy.state != FileState.AVAILABLE:
        copy.arrived_at = utcnow()  # the rule made the destination copy available (SVC-29)
    copy.size, copy.mtime = size, mtime
    copy.hash = hash if available else None
    copy.last_seen_at = utcnow()
    tr.release(copy, FileState.AVAILABLE if available else FileState.UNKNOWN)
    tr.bump(copy)
    session.flush()
    _recompute(session, copy.file_id)
    return copy_snapshot(session, copy)


def finish_transfer(
    session: Session,
    plan: TransferSnapshot,
    snap: CopySnapshot,
    *,
    size: int,
    mtime: datetime | None,
    hash: str,
) -> CopySnapshot:
    """Publish a verified copy, never replacing the logical file's accepted hash."""
    guard_source(session, plan.source, plan.source_url)
    guard_destination(session, plan, snap)
    state = uploads.finish_new(session, snap, size=size, mtime=mtime, hash=hash)
    if state != FileState.AVAILABLE:
        raise tr.StaleOperation("the verified transfer no longer matches the logical file")
    # A rule transfer is an arrival, even into a reused (e.g. deleted) copy row (SVC-29).
    session.get_one(FileStorageLocation, snap.copy_id).arrived_at = utcnow()
    session.flush()
    return copy_snapshot(session, session.get_one(FileStorageLocation, snap.copy_id))


def begin_source_delete(
    session: Session,
    snap: CopySnapshot,
    url: str,
    lease: timedelta,
    *,
    destination: CopySnapshot | None = None,
) -> CopySnapshot:
    """Own only the expected source revision, after destination verification for a move.

    For a move, ``destination`` (the verified destination copy) is recorded on
    the source copy, so a repair retrying an interrupted delete re-checks it.
    """
    copy = guard_source(session, snap, url)
    tr.take(copy, FileState.DELETING, uploads.new_token(), tr.clock() + lease)
    tr.bump(copy)
    if destination is not None:
        copy.move_destination_id = destination.copy_id
    session.flush()
    _recompute(session, snap.file_id)
    return copy_snapshot(session, copy)


def check_move_destination(session: Session, snap: CopySnapshot) -> CopySnapshot:
    """Check that a move's recorded destination still holds the moved content.

    ``snap`` is the source copy, owned by a repair retrying its delete. Its
    recorded destination copy must belong to the same file, be ``available``
    with no operation on it, and the file must still have the source's hash.

    Returns:
        A snapshot of the destination copy.

    Raises:
        StaleOperation: if the source is no longer owned or any check fails.
    """
    tr.lock_copy(
        session,
        snap.copy_id,
        token=snap.token,
        generation=snap.generation,
        states={FileState.DELETING},
        expected_hash=snap.file_hash,
    )
    if snap.move_destination_id is None:
        raise tr.StaleOperation(f"copy {snap.copy_id} records no move destination")
    destination = tr.lock_copy(
        session,
        snap.move_destination_id,
        states={FileState.AVAILABLE},
        expected_hash=snap.file_hash,
    )
    if destination.file_id != snap.file_id:
        raise tr.StaleOperation("the recorded move destination belongs to another file")
    return copy_snapshot(session, destination)


def abandon_source_delete(session: Session, snap: CopySnapshot) -> None:
    """Settle a move's source delete without deleting: the copy is re-checked later.

    Used when a repair cannot confirm the move's destination. The copy becomes
    token-less ``pending`` (known content awaiting verification), which the
    next full index re-hashes.
    """
    copy = tr.lock_copy(
        session,
        snap.copy_id,
        token=snap.token,
        generation=snap.generation,
        states={FileState.DELETING},
    )
    tr.release(copy, FileState.PENDING)
    tr.bump(copy)
    session.flush()
    _recompute(session, snap.file_id)


def finish_source_delete(
    session: Session, snap: CopySnapshot, outcome: FileState, *, user: str | None = None
) -> None:
    """Settle one delete; soft-delete the logical file only when every copy is deleted."""
    if outcome not in {FileState.DELETED, FileState.UNKNOWN, FileState.DELETING}:
        raise ValueError(f"invalid source-delete outcome {outcome}")
    copy = tr.lock_copy(
        session,
        snap.copy_id,
        token=snap.token,
        generation=snap.generation,
        states={FileState.DELETING},
    )
    tr.release(copy, outcome)
    if outcome == FileState.DELETED:
        tr.bump(copy)
    file = session.get_one(File, snap.file_id)
    session.flush()
    live = session.scalar(
        select(FileStorageLocation.id)
        .where(
            FileStorageLocation.file_id == snap.file_id,
            FileStorageLocation.state != FileState.DELETED,
        )
        .limit(1)
    )
    if live is None:
        file.deleted_at = utcnow()
    if outcome == FileState.DELETED:
        file.updated_at = utcnow()
        file.updated_by = user
    _recompute(session, snap.file_id)

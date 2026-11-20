"""In-memory state and internals of the fake client."""

from __future__ import annotations

import shutil
import threading
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from heliopuddle.client import BusyError, FileRecord, HelioPuddleError, NotFoundError
from heliopuddle.server.schemas import FileState, FileStorageLocationRead
from heliopuddle.testing._helpers import (
    LATEST_ELIGIBLE,
    Call,
    aware,
    md5,
    mtime,
    utcnow,
    version_kind,
)


class FakeState:
    """Attributes and internals shared by the fake's call groups."""

    project: str
    store: Path | None
    storage_locations: list[str]
    user: str | None
    calls: list[Call]
    closed: bool
    _files: dict[int, FileRecord]
    _ids: Iterator[int]
    _busy: dict[str, list[float | None]]
    _errors: dict[tuple[str, str | None], list[BaseException]]
    _series_kinds: dict[str, str]
    _lock: threading.RLock

    def _enter(self, method: str, target: str | int | None, changes: bool = False) -> None:
        self.calls.append(Call(method, target))
        key_path = target if isinstance(target, str) else None
        for key in ((method, key_path), (method, None)):
            queued = self._errors.get(key)
            if queued:
                raise queued.pop(0)
        if changes and isinstance(target, str) and self._busy.get(target):
            raise BusyError(f"{target!r} is owned by another operation", self._busy[target].pop(0))

    def _live(self, path: str) -> FileRecord | None:
        for record in self._files.values():
            if record.path == path and record.deleted_at is None:
                return record
        return None

    def _by_id(self, file_id: int, live_only: bool) -> FileRecord:
        record = self._files.get(file_id)
        if record is None or (live_only and record.deleted_at is not None):
            raise NotFoundError(404, f"file {file_id} not found")
        return record

    def _resolve(self, path_or_id: str | int) -> FileRecord:
        if isinstance(path_or_id, int):
            return self._by_id(path_or_id, live_only=True)
        record = self._live(path_or_id)
        if record is None:
            raise NotFoundError(404, f"no live file at {path_or_id!r}")
        return record

    def _location(self, name: str | None) -> str:
        if name is None:
            return self.storage_locations[0]
        if name not in self.storage_locations:
            raise NotFoundError(404, f"storage location {name!r} not found")
        return name

    def _target(self, location: str, path: str) -> Path | None:
        """The object for ``path`` in ``location``, or None if the fake has no store there."""
        if self.store is None or location != self.storage_locations[0]:
            return None
        return self.store / path

    def _verified(self, location: str, path: str, hash: str, size: int) -> FileStorageLocationRead:
        """A copy as the server's verify leaves it: observed now, ``available`` if it matches."""
        now = utcnow()
        target = self._target(location, path)
        if target is None:  # no store here: trusted
            return FileStorageLocationRead(
                storage_location=location,
                state=FileState.AVAILABLE,
                size=size,
                hash=hash,
                last_seen_at=now,
                updated_at=now,
            )
        if not target.is_file():
            return FileStorageLocationRead(
                storage_location=location, state=FileState.UNKNOWN, updated_at=now
            )
        digest, observed = md5(target)
        match = (digest, observed) == (hash, size)
        return FileStorageLocationRead(
            storage_location=location,
            state=FileState.AVAILABLE if match else FileState.UNKNOWN,
            size=observed,
            mtime=mtime(target),
            hash=digest if match else None,
            last_seen_at=now,
            updated_at=now,
        )

    def _delete_object(self, path: str, copy: FileStorageLocationRead) -> FileState:
        """Delete one copy's object if it is still what was last observed; return its state."""
        target = self._target(copy.storage_location, path)
        if target is None or not target.is_file():
            return FileState.DELETED
        if copy.size is None or copy.last_seen_at is None:
            return FileState.UNKNOWN  # never observed: left in the store
        stat = target.stat()
        if stat.st_size != copy.size or (copy.mtime is not None and mtime(target) != copy.mtime):
            return FileState.UNKNOWN  # changed since observed: left in the store
        try:
            target.unlink(missing_ok=True)
        except OSError:
            return FileState.DELETING
        return FileState.DELETED

    def _write(self, location: str, path: str, source: Path) -> None:
        target = self._target(location, path)
        if target is None or target.resolve() == source.resolve():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)

    def _create(
        self,
        path: str,
        hash: str,
        size: int,
        metadata: dict[str, Any] | None,
        content_date: datetime | None,
        copy: FileStorageLocationRead,
        version: Version = (None, None),
    ) -> FileRecord:
        self._check_series_kind(path, version)
        kind = version_kind(version)
        if kind is not None:
            self._series_kinds.setdefault(path, kind)
        file_id = next(self._ids)
        now = utcnow()
        record = FileRecord(
            id=file_id,
            project=self.project,
            path=path,
            name=path.rsplit("/", 1)[-1],
            series_id=file_id,
            version_number=version[0],
            version_text=version[1],
            content_date=aware(content_date),
            size=size,
            hash=hash,
            hash_algo="md5",
            metadata=metadata,
            is_latest=False,
            created_at=now,
            updated_at=now,
            created_by=self.user,
            storage_locations=[copy],
        )
        return self._put(record)

    def _check_series_kind(self, path: str, version: Version) -> None:
        """Refuse a version of the other kind than the path's series (``422``, API-21)."""
        kind, series_kind = version_kind(version), self._series_kinds.get(path)
        if kind is not None and series_kind is not None and kind != series_kind:
            raise HelioPuddleError(
                422, f"the series of {path!r} has {series_kind} versions, not {kind}"
            )

    def _set_copy(self, record: FileRecord, copy: FileStorageLocationRead) -> FileRecord:
        """Replace the record's copy in ``copy.storage_location``, or add it."""
        copies = [
            c for c in record.storage_locations if c.storage_location != copy.storage_location
        ]
        return self._put(record.model_copy(update={"storage_locations": [*copies, copy]}))

    def _put(self, record: FileRecord) -> FileRecord:
        """Store ``record`` with copies in storage-location order and ``is_latest`` recomputed."""
        copies = sorted(
            record.storage_locations, key=lambda c: self.storage_locations.index(c.storage_location)
        )
        latest = record.deleted_at is None and any(c.state in LATEST_ELIGIBLE for c in copies)
        update: dict[str, Any] = {"storage_locations": copies, "is_latest": latest}
        if latest != record.is_latest and record.id in self._files:
            update["updated_at"] = utcnow()
        record = record.model_copy(update=update)
        self._files[record.id] = record
        return record


Version = tuple[int | None, str | None]

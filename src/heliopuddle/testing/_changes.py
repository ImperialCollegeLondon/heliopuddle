"""The changing calls of the fake: register, add, remove and edit files."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from heliopuddle.client import ConflictError, FileRecord, HelioPuddleError
from heliopuddle.server.schemas import FileState
from heliopuddle.testing._helpers import (
    aware,
    check_same_version,
    copy_of,
    md5,
    mtime,
    parse_version,
    utcnow,
    validate_path,
)
from heliopuddle.testing._state import FakeState


class ChangesMixin(FakeState):
    """``register_file``, ``add_file``, ``remove_file`` and the record edits."""

    def register_file(
        self,
        path: str,
        hash: str,
        size: int,
        storage_location: str | None = None,
        metadata: dict[str, Any] | None = None,
        *,
        content_date: datetime | None = None,
        version_number: int | None = None,
        version_text: str | None = None,
    ) -> FileRecord:
        """Like ``POST /projects/{p}/files``: record a file already in the store.

        A new file gets the version override, if any (the fake has no version
        rules). At a live path with the same hash the existing record is
        returned (``metadata`` and ``content_date`` ignored); its copy in
        ``storage_location`` is re-verified unless it is ``available``, or
        added if the file has none there. An override different from the live
        file's version raises :class:`ConflictError`.

        Raises:
            ValueError: both ``version_number`` and ``version_text`` were given.
        """
        version = parse_version(version_number, version_text)
        self._enter("register_file", path, changes=True)
        with self._lock:
            validate_path(path)
            location = self._location(storage_location)
            existing = self._live(path)
            if existing is None:
                self._check_series_kind(path, version)
                copy = self._verified(location, path, hash, size)
                return self._create(path, hash, size, metadata, content_date, copy, version)
            check_same_version(existing, version)
            if existing.hash != hash:
                raise ConflictError(
                    f"a different file exists at {path!r} (hash {existing.hash})", existing
                )
            current = copy_of(existing, location)
            if current is not None and current.state == FileState.AVAILABLE:
                return existing
            copy = self._verified(location, path, existing.hash, existing.size)
            return self._set_copy(existing, copy)

    def add_file(
        self,
        local_path: str | Path,
        path: str,
        storage_location: str | None = None,
        metadata: dict[str, Any] | None = None,
        *,
        overwrite: bool = False,
        content_date: datetime | None = None,
        version_number: int | None = None,
        version_text: str | None = None,
    ) -> FileRecord:
        """Like the client's upload: hash ``local_path`` and store it at ``path``.

        A new file gets the version override, if any. A live file keeps its
        version: an override different from it raises :class:`ConflictError`.

        Raises:
            ValueError: both ``version_number`` and ``version_text`` were given.
        """
        version = parse_version(version_number, version_text)
        source = Path(local_path)
        if not source.is_file():
            raise FileNotFoundError(f"not a file: {source}")
        self._enter("add_file", path, changes=True)
        with self._lock:
            validate_path(path)
            location = self._location(storage_location)
            digest, size = md5(source)
            existing = self._live(path)
            if existing is None:
                self._check_series_kind(path, version)
            else:
                check_same_version(existing, version)
                copy = copy_of(existing, location)
                if existing.hash == digest and copy is not None and copy.state == "available":
                    return existing
                if existing.hash != digest and not overwrite:
                    raise ConflictError(
                        f"a different file exists at {path!r} (hash {existing.hash})", existing
                    )
                if existing.hash != digest:
                    if copy is None or copy.state != FileState.AVAILABLE:
                        raise HelioPuddleError(
                            409, f"only an available copy can be overwritten: {path!r}"
                        )
                    self._write(location, path, source)
                    now = utcnow()
                    written = self._verified(location, path, digest, size)
                    others = [
                        written
                        if c.storage_location == location
                        else c.model_copy(update={"state": FileState.UNKNOWN, "updated_at": now})
                        for c in existing.storage_locations
                    ]
                    update: dict[str, Any] = {
                        "hash": digest,
                        "size": size,
                        "updated_at": now,
                        "updated_by": self.user,
                        "storage_locations": others,
                    }
                    if metadata is not None:
                        update["metadata"] = metadata
                    if content_date is not None:
                        update["content_date"] = aware(content_date)
                    return self._put(existing.model_copy(update=update))
            self._write(location, path, source)
            if existing is not None:  # same hash, copy missing or not available: written again
                return self._set_copy(
                    existing, self._verified(location, path, existing.hash, existing.size)
                )
            copy = self._verified(location, path, digest, size)
            return self._create(path, digest, size, metadata, content_date, copy, version)

    def remove_file(self, path_or_id: str | int) -> FileRecord:
        """Like ``DELETE /files/{id}``: delete every copy, then soft-delete the file.

        An object is deleted only if it is still what was last observed (size
        and mtime). One that changed, or was never observed, is left in the
        store with its copy ``unknown``; one whose delete fails stays
        ``deleting``. The file is soft-deleted only when every copy is
        ``deleted``; otherwise the live record is returned.
        """
        record = self._resolve(path_or_id)
        self._enter("remove_file", record.path, changes=True)
        with self._lock:
            now = utcnow()
            copies = [
                c
                if c.state == FileState.DELETED
                else c.model_copy(
                    update={"state": self._delete_object(record.path, c), "updated_at": now}
                )
                for c in record.storage_locations
            ]
            update: dict[str, Any] = {
                "updated_at": now,
                "storage_locations": copies,
                "updated_by": self.user,
            }
            if all(c.state == FileState.DELETED for c in copies):
                update["deleted_at"] = now
            return self._put(record.model_copy(update=update))

    def update_metadata(
        self, file_id: int, patch: dict[str, Any], replace: bool = False
    ) -> FileRecord:
        """Like ``PATCH /files/{id}/metadata``: merge (``None`` removes a key) or replace."""
        self._enter("update_metadata", file_id, changes=True)
        with self._lock:
            record = self._by_id(file_id, live_only=True)
            if replace:
                metadata = {k: v for k, v in patch.items() if v is not None}
            else:
                metadata = dict(record.metadata or {})
                for key, value in patch.items():
                    if value is None:
                        metadata.pop(key, None)
                    else:
                        metadata[key] = value
            return self._put(
                record.model_copy(
                    update={"metadata": metadata, "updated_at": utcnow(), "updated_by": self.user}
                )
            )

    def set_content_date(self, path_or_id: str | int, content_date: datetime | None) -> FileRecord:
        """Like ``PATCH /files/{id}/content-date``; a naive time is UTC.

        The fake has no version rules, so ``None`` clears the date (the server
        would go back to a date in the path, if a rule takes one).
        """
        record = self._resolve(path_or_id)
        self._enter("set_content_date", record.path, changes=True)
        with self._lock:
            record = self._by_id(record.id, live_only=True)
            return self._put(
                record.model_copy(
                    update={
                        "content_date": aware(content_date),
                        "updated_at": utcnow(),
                        "updated_by": self.user,
                    }
                )
            )

    def accept_file(self, path_or_id: str | int, storage_location: str | None = None) -> FileRecord:
        """Like ``POST /files/{id}/accept``: take an ``unknown`` copy's content as it is now."""
        record = self._resolve(path_or_id)
        self._enter("accept_file", record.path, changes=True)
        with self._lock:
            unknown = [c for c in record.storage_locations if c.state == FileState.UNKNOWN]
            if storage_location is None:
                if len(unknown) != 1:
                    raise HelioPuddleError(422, "name the storage location of the copy to accept")
                location = unknown[0].storage_location
            else:
                location = self._location(storage_location)
                if not any(c.storage_location == location for c in unknown):
                    raise HelioPuddleError(409, f"the copy in {location!r} is not unknown")
            if self.store is None or location != self.storage_locations[0]:
                raise HelioPuddleError(409, "the fake can only accept copies in its store")
            target = self.store / record.path
            if not target.is_file():
                raise HelioPuddleError(409, f"the object at {record.path!r} is missing")
            digest, size = md5(target)
            now = utcnow()
            copies = [
                c.model_copy(
                    update={
                        "state": FileState.AVAILABLE,
                        "hash": digest,
                        "size": size,
                        "mtime": mtime(target),
                        "last_seen_at": now,
                        "updated_at": now,
                    }
                )
                if c.storage_location == location
                else (
                    c.model_copy(update={"state": FileState.UNKNOWN, "updated_at": now})
                    if digest != record.hash and c.state == FileState.AVAILABLE
                    else c
                )
                for c in record.storage_locations
            ]
            return self._put(
                record.model_copy(
                    update={
                        "hash": digest,
                        "size": size,
                        "storage_locations": copies,
                        "updated_at": now,
                        "updated_by": self.user,
                    }
                )
            )

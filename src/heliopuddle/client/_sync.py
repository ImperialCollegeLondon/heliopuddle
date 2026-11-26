"""Keeping a path in heliopuddle in step with a local file (CLI-18).

Built only on the client's public file methods, so the real client and
:class:`heliopuddle.testing.FakeHelioPuddleClient` share the same code.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum, StrEnum
from pathlib import Path
from typing import Any

from heliopuddle.client.errors import ConflictError, NotFoundError, SyncError
from heliopuddle.server.schemas import FileRead, FileState
from heliopuddle.storage.hashing import file_digest


class SyncAction(StrEnum):
    """What a sync did, or in a dry run would do."""

    ADDED = "added"
    """A new file was registered or uploaded."""
    UPDATED = "updated"
    """The live file got the new content."""
    METADATA = "metadata"
    """Same content; the metadata or content date was changed."""
    REMOVED = "removed"
    """The live file was removed."""
    UNCHANGED = "unchanged"
    """Nothing needed to change."""


@dataclass(frozen=True)
class SyncResult:
    """The outcome of :meth:`SyncMixin.sync_file` or :meth:`SyncMixin.sync_removed`.

    Attributes:
        action: what was done (or, in a dry run, would be done).
        file: the record afterwards; in a dry run, or when nothing was live,
            the record as it was (None if there was none).
    """

    action: SyncAction
    file: FileRead | None


class _Unset(Enum):
    UNSET = "unset"


UNSET = _Unset.UNSET
"""Default of ``current``: look the live file up."""


class SyncMixin:
    """``sync_file`` and ``sync_removed`` of the client and the fake (CLI-18)."""

    get_file_by_path: Callable[..., FileRead]
    register_file: Callable[..., FileRead]
    add_file: Callable[..., FileRead]
    accept_file: Callable[..., FileRead]
    update_metadata: Callable[..., FileRead]
    set_content_date: Callable[..., FileRead]
    remove_file: Callable[..., FileRead]

    def sync_file(
        self,
        local_path: str | Path,
        path: str,
        storage_location: str | None = None,
        metadata: dict[str, Any] | None = None,
        *,
        in_place: bool = False,
        content_date: datetime | None = None,
        current: FileRead | _Unset | None = UNSET,
        dry_run: bool = False,
    ) -> SyncResult:
        """Make the live file at ``path`` hold ``local_path``'s content.

        The local file is hashed (md5 and size, one read) and compared with the
        live record:

        - none: the file is added, with :meth:`register_file` when
          ``in_place`` (the content is already at ``path`` in the storage
          location, e.g. a store shared with the caller) or :meth:`add_file`
          otherwise. If another writer added different content meanwhile, it
          is replaced as below;
        - a different hash: the content is replaced. With ``in_place`` and a
          copy heliopuddle already found changed (``unknown``), the copy is
          accepted as it is; otherwise the file is uploaded with
          ``overwrite=True``;
        - the same hash: only the metadata and content date are brought in line.

        Afterwards the record must have the local hash and an ``available``
        copy, and ``metadata`` (if given) replaces the record's metadata and
        ``content_date`` (if given) is set. A busy path is retried as the
        client's ``busy_retries`` says.

        Args:
            local_path: the local file with the wanted content.
            path: relative, ``/``-separated path in the store.
            storage_location: storage location name; None means the project's first.
            metadata: the file's whole metadata; None leaves it as it is.
            in_place: the content is already at ``path`` in the storage location.
            content_date: the file's content date; None leaves it as it is.
            current: the live record at ``path`` when the caller already has it
                (e.g. from :meth:`find_all`), or None if there is none; looked up
                when omitted.
            dry_run: work out the action without changing anything.

        Returns:
            What was (or would be) done, and the record.

        Raises:
            FileNotFoundError: ``local_path`` is not a file.
            SyncError: the record does not have the local hash, or its copy is
                not ``available`` (e.g. the store object did not match).
            BusyError: the path was still busy after the retries.
            HelioPuddleError: any other API error.
        """
        source = Path(local_path)
        if not source.is_file():
            raise FileNotFoundError(f"not a file: {source}")
        digest, size = file_digest(source)
        if current is UNSET:
            current = self._live_or_none(path)

        if current is not None and current.hash == digest:
            if not _needs_record_changes(current, metadata, content_date):
                return SyncResult(SyncAction.UNCHANGED, current)
            if dry_run:
                return SyncResult(SyncAction.METADATA, current)
            changed = self._apply_record_changes(current, metadata, content_date)
            return SyncResult(SyncAction.METADATA, changed)
        if dry_run:
            action = SyncAction.ADDED if current is None else SyncAction.UPDATED
            return SyncResult(action, current)

        put = _Put(source, path, digest, size, storage_location, metadata, content_date, in_place)
        record: FileRead | None = None
        action = SyncAction.UPDATED
        if current is None:
            try:
                record = self._sync_new(put)
                action = SyncAction.ADDED
            except ConflictError as conflict:  # another writer added other content meanwhile
                current = conflict.file
        if record is None:
            assert current is not None
            record = self._sync_replace(put, current)

        _check_synced(record, put)
        return SyncResult(action, self._apply_record_changes(record, metadata, content_date))

    def sync_removed(
        self,
        path: str,
        *,
        current: FileRead | _Unset | None = UNSET,
        dry_run: bool = False,
    ) -> SyncResult:
        """Make sure no live file is at ``path``, removing it if there is one.

        Args:
            path: relative, ``/``-separated path in the store.
            current: the live record at ``path`` when the caller already has it,
                or None if there is none; looked up when omitted.
            dry_run: work out the action without changing anything.

        Returns:
            ``removed`` with the deleted record, or ``unchanged`` when nothing
            was live at ``path``.

        Raises:
            SyncError: the file is still live after the delete (a store delete
                failed, or a changed object was kept as ``unknown``).
            BusyError: the path was still busy after the retries.
            HelioPuddleError: any other API error.
        """
        if current is UNSET:
            current = self._live_or_none(path)
        if current is None:
            return SyncResult(SyncAction.UNCHANGED, None)
        if dry_run:
            return SyncResult(SyncAction.REMOVED, current)
        try:
            record = self.remove_file(current.id)
        except NotFoundError:  # removed by another writer meanwhile
            return SyncResult(SyncAction.UNCHANGED, None)
        if record.deleted_at is None:
            states = ", ".join(f"{c.storage_location}={c.state}" for c in record.storage_locations)
            raise SyncError(f"{path!r} is still live after the delete: {states}", record)
        return SyncResult(SyncAction.REMOVED, record)

    # -- helpers -------------------------------------------------------------

    def _live_or_none(self, path: str) -> FileRead | None:
        try:
            return self.get_file_by_path(path)
        except NotFoundError:
            return None

    def _sync_new(self, put: _Put) -> FileRead:
        if put.in_place:
            return self.register_file(
                put.path,
                put.digest,
                put.size,
                put.storage_location,
                put.metadata,
                content_date=put.content_date,
            )
        return self.add_file(
            put.source,
            put.path,
            put.storage_location,
            put.metadata,
            content_date=put.content_date,
        )

    def _sync_replace(self, put: _Put, current: FileRead) -> FileRead:
        if put.in_place and copy_state(current, put.storage_location) == FileState.UNKNOWN:
            # heliopuddle has already seen the new content in the store.
            accepted = self.accept_file(current.id, storage_location=put.storage_location)
            if accepted.hash == put.digest:
                return accepted
        return self.add_file(
            put.source,
            put.path,
            put.storage_location,
            put.metadata,
            overwrite=True,
            content_date=put.content_date,
        )

    def _apply_record_changes(
        self, record: FileRead, metadata: dict[str, Any] | None, content_date: datetime | None
    ) -> FileRead:
        if metadata is not None and record.metadata != metadata:
            record = self.update_metadata(record.id, metadata, replace=True)
        if content_date is not None and _utc(record.content_date) != _utc(content_date):
            record = self.set_content_date(record.id, content_date)
        return record


@dataclass(frozen=True)
class _Put:
    """What :meth:`SyncMixin.sync_file` puts at a path."""

    source: Path
    path: str
    digest: str
    size: int
    storage_location: str | None
    metadata: dict[str, Any] | None
    content_date: datetime | None
    in_place: bool


def copy_state(record: FileRead, storage_location: str | None) -> FileState | None:
    """The state of the record's copy in ``storage_location`` (None: its first copy)."""
    for copy in record.storage_locations:
        if storage_location is None or copy.storage_location == storage_location:
            return copy.state
    return None


def _check_synced(record: FileRead, put: _Put) -> None:
    if record.hash != put.digest:
        raise SyncError(
            f"heliopuddle has hash {record.hash} for {put.path!r}, expected {put.digest}", record
        )
    state = copy_state(record, put.storage_location)
    if state != FileState.AVAILABLE:
        raise SyncError(f"the copy of {put.path!r} is {state}, expected available", record)


def _needs_record_changes(
    record: FileRead, metadata: dict[str, Any] | None, content_date: datetime | None
) -> bool:
    if metadata is not None and record.metadata != metadata:
        return True
    return content_date is not None and _utc(record.content_date) != _utc(content_date)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)

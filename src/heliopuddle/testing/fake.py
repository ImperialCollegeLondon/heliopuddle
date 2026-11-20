"""An in-memory stand-in for :class:`heliopuddle.client.HelioPuddleClient` (CLI-14).

Applications that call heliopuddle test against :class:`FakeHelioPuddleClient`
instead of a server: it has the client's file methods with the same
signatures, records and errors, keeps its state in memory, and needs only the
core (client) install. A contract test in heliopuddle runs the same scenarios
against the fake and the real API, so the two behave alike for those calls.

Like the server, each copy records hp's last observation of its object
(``size``, ``mtime``, ``hash``, ``last_seen_at``): registering the same hash
again re-verifies a copy that is not ``available`` (or adds the copy in another
storage location), and ``remove_file`` deletes an object only if it is still
what was last observed. A changed or never-observed object is left in the
store with its copy ``unknown`` and the file stays live; a store delete that
fails leaves the copy ``deleting``.

Simplifications (documented, checked by the contract tests where they matter):

- one project; storage locations are named up front (default ``primary``);
- there are no version rules: a file has the version given as
  ``version_number`` or ``version_text`` when it was created, else none, and
  every path is its own series, so a live file is latest when it has an
  ``available`` copy. As on the server, a path's series keeps the kind
  (number or text) of its first versioned file, and a live file's version
  never changes;
- verification is immediate and only the first storage location has a store:
  with a ``store`` directory a copy there is ``available`` when the object
  matches the file's hash and size, else ``unknown``; copies in other storage
  locations, and every copy without a store, are trusted as ``available``;
- deletes compare size and mtime with the last observation (as the server does
  on a local store) and never retry: a ``deleting`` copy stays so;
- nothing is ever busy unless a test asks for it (:meth:`inject_busy`);
- ``health()`` always reports every part ``ok``.
"""

from __future__ import annotations

import hashlib
import itertools
import threading
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from heliopuddle.client import FileRecord, HelioPuddleError, NotFoundError
from heliopuddle.client.paging import FindAllMixin, Order
from heliopuddle.server.schemas import (
    FileCheckResult,
    FileList,
    FileState,
    HealthResponse,
    StorageLocationRead,
)
from heliopuddle.testing._changes import ChangesMixin
from heliopuddle.testing._helpers import Call, FindFilter, matches, parse_version, validate_path


class FakeHelioPuddleClient(ChangesMixin, FindAllMixin):
    """In-memory heliopuddle for tests; a drop-in for ``HelioPuddleClient``.

    Args:
        project: the project name every record reports.
        store: optional directory holding the first storage location's objects.
            ``register_file`` verifies against it, ``add_file`` writes to it and
            ``remove_file`` deletes from it, like a real local storage location.
        storage_locations: names of the project's storage locations; the first
            is the default.
        user: recorded as ``created_by``/``updated_by``.

    Test helpers: :attr:`calls` (every call in order), :meth:`seed`,
    :meth:`inject_busy`, :meth:`inject_error`, :meth:`record_at`,
    :meth:`live_paths`, :meth:`methods_called` and :meth:`reset_calls`.
    """

    def __init__(
        self,
        project: str = "default",
        store: str | Path | None = None,
        storage_locations: Sequence[str] = ("primary",),
        user: str | None = None,
    ) -> None:
        if not storage_locations:
            raise ValueError("at least one storage location is needed")
        self.project = project
        self.store = Path(store) if store is not None else None
        self.storage_locations = list(storage_locations)
        self.user = user
        self.calls: list[Call] = []
        self.closed = False
        self._files: dict[int, FileRecord] = {}
        self._ids = itertools.count(1)
        self._busy: dict[str, list[float | None]] = {}
        self._errors: dict[tuple[str, str | None], list[BaseException]] = {}
        self._series_kinds: dict[str, str] = {}
        self._lock = threading.RLock()

    # -- lifecycle -------------------------------------------------------------

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> FakeHelioPuddleClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- test helpers ----------------------------------------------------------

    def seed(
        self,
        path: str,
        content: bytes | None = None,
        *,
        hash: str | None = None,
        size: int | None = None,
        metadata: dict[str, Any] | None = None,
        state: FileState | str = FileState.AVAILABLE,
        storage_location: str | None = None,
        content_date: datetime | None = None,
        version_number: int | None = None,
        version_text: str | None = None,
    ) -> FileRecord:
        """Put a live file in place without recording a call.

        Give ``content`` (also written to the store when the copy is in the
        first storage location and there is one) or a ``hash`` and ``size``.
        ``state`` sets the copy state, e.g. ``unknown`` to stand for content
        that changed behind heliopuddle's back. An object in the store at
        ``path`` counts as observed as it is now. The path, storage location
        and live path are checked before anything is written.

        Raises:
            HelioPuddleError: ``422`` for an invalid path.
            NotFoundError: unknown storage location.
            ValueError: a live file already exists at ``path``, neither
                ``content`` nor ``hash`` and ``size`` were given, or both
                ``version_number`` and ``version_text`` were given.
        """
        version = parse_version(version_number, version_text)
        with self._lock:
            validate_path(path)
            location = self._location(storage_location)
            if self._live(path) is not None:
                raise ValueError(f"a live file already exists at {path!r}")
            self._check_series_kind(path, version)
            if content is not None:
                hash = hashlib.md5(content).hexdigest()
                size = len(content)
            if hash is None or size is None:
                raise ValueError("seed needs content, or hash and size")
            target = self._target(location, path)
            if content is not None and target is not None:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            copy = self._verified(location, path, hash, size)
            copy = copy.model_copy(update={"state": FileState(state)})
            if copy.state != FileState.AVAILABLE:
                copy = copy.model_copy(update={"hash": None})
            return self._create(path, hash, size, metadata, content_date, copy, version)

    def inject_busy(self, path: str, times: int = 1, retry_after: float | None = 0.0) -> None:
        """Make the next ``times`` changing calls on ``path`` raise :class:`BusyError`."""
        self._busy.setdefault(path, []).extend([retry_after] * times)

    def inject_error(
        self, method: str, error: BaseException, path: str | None = None, times: int = 1
    ) -> None:
        """Make the next ``times`` calls of ``method`` (on ``path``, or any) raise ``error``."""
        self._errors.setdefault((method, path), []).extend([error] * times)

    def record_at(self, path: str) -> FileRecord | None:
        """The live record at ``path``, or None; not recorded as a call."""
        return self._live(path)

    def live_paths(self) -> list[str]:
        """Paths of every live file, sorted."""
        return sorted(f.path for f in self._files.values() if f.deleted_at is None)

    def reset_calls(self) -> None:
        self.calls.clear()

    def methods_called(self) -> list[str]:
        """The method name of every recorded call, in order."""
        return [c.method for c in self.calls]

    # -- client API: reads -------------------------------------------------------

    def health(self) -> HealthResponse:
        """Like ``GET /health`` on a healthy server: every part is ``ok``."""
        self._enter("health", None)
        return HealthResponse(status="ok", database="ok", prefect="ok")

    def list_storage_locations(self) -> list[StorageLocationRead]:
        """The fake's storage locations, all active, in the order given."""
        self._enter("list_storage_locations", None)
        return [
            StorageLocationRead(name=name, url="", accept_external_changes=True, active=True)
            for name in self.storage_locations
        ]

    def get_file(self, file_id: int) -> FileRecord:
        self._enter("get_file", file_id)
        return self._by_id(file_id, live_only=False)

    def get_file_by_path(self, path: str) -> FileRecord:
        self._enter("get_file_by_path", path)
        record = self._live(path)
        if record is None:
            raise NotFoundError(404, f"no live file at {path!r}")
        return record

    def check(self, path: str, hash: str) -> FileCheckResult:
        self._enter("check", path)
        record = self._live(path)
        return FileCheckResult(
            exists=record is not None,
            hash_matches=record is not None and record.hash == hash,
            file=record,
        )

    def list_files(self, limit: int = 100, offset: int = 0) -> FileList:
        return self.find(limit=limit, offset=offset)

    def find(
        self,
        path: str | None = None,
        latest_only: bool = False,
        state: FileState | str | Sequence[FileState | str] | None = None,
        storage_location: str | None = None,
        content_date_from: datetime | None = None,
        content_date_to: datetime | None = None,
        include_deleted: bool = False,
        series_id: int | None = None,
        deleted_only: bool = False,
        limit: int = 100,
        offset: int = 0,
        order: Order = "-created_at",
        ignored_from_all_rules: bool | None = None,
    ) -> FileList:
        """Filter like ``GET /projects/{p}/files`` (every live file is latest here)."""
        self._enter("find", path)
        if not 1 <= limit <= 1000:
            raise HelioPuddleError(422, "limit must be between 1 and 1000")
        states = None
        if state is not None:
            states = frozenset(str(s) for s in ([state] if isinstance(state, str) else state))
        flt = FindFilter(
            path=path,
            latest_only=latest_only,
            states=states,
            storage_location=storage_location,
            content_date_from=content_date_from,
            content_date_to=content_date_to,
            include_deleted=include_deleted,
            series_id=series_id,
            deleted_only=deleted_only,
            ignored_from_all_rules=ignored_from_all_rules,
        )
        items = [record for record in self._files.values() if matches(record, flt)]
        if order == "path":
            items.sort(key=lambda r: (r.path, r.id))
        else:
            items.sort(key=lambda r: (r.created_at, r.id), reverse=True)
        return FileList(
            items=items[offset : offset + limit], total=len(items), limit=limit, offset=offset
        )

    def latest(self, path: str) -> FileRecord:
        """The live file at ``path`` if it is latest (has an ``available`` copy)."""
        record = self.get_file_by_path(path)
        if not record.is_latest:
            raise NotFoundError(404, f"the series of {path!r} has no live version")
        return record

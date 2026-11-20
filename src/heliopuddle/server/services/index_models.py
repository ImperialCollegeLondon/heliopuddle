"""Index data types: listed entries, counts and summaries (SVC-03, SVC-08, SVC-10).

Shared by the index flows (``index.py``), the per-batch diff
(``index_diff.py``) and the tasks (``index_tasks.py``).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from heliopuddle.server.services.index_names import IndexMode
from heliopuddle.storage.location import ListEntry


class IndexEntry(BaseModel):
    """One listed file passed to :func:`index_batch` (serialisable for Prefect).

    Attributes:
        path: relative path in the storage location.
        size: size in bytes.
        mtime: modification time, if the backend reports one.
        md5: the backend's own md5, if the listing reports one (D6).
        touch_only: the entry is only recorded as seen (``last_seen_at``),
            not diffed: it is inside the settle time (full index only).
        temp: the entry is a ``*.hp-tmp`` file, swept if no operation owns it
            (full index only).
    """

    path: str
    size: int
    mtime: datetime | None = None
    md5: str | None = None
    touch_only: bool = False
    temp: bool = False
    skip: bool = False

    @classmethod
    def from_listing(
        cls, entry: ListEntry, touch_only: bool = False, temp: bool = False
    ) -> IndexEntry:
        """Build from a :class:`~heliopuddle.storage.location.ListEntry`."""
        return cls(
            path=entry.path,
            size=entry.size,
            mtime=entry.mtime,
            md5=entry.md5,
            touch_only=touch_only,
            temp=temp,
        )


class IndexCounts(BaseModel):
    """Counts of what an index run (or one batch) did.

    Attributes:
        listed: files listed in the store (including skipped ones).
        added: new files, and new copies of known files.
        changed: files whose accepted content (hash) changed.
        removed: copies found gone and marked ``deleted``.
        missing: copies found gone and marked ``missing``.
        unknown: copies newly recorded as ``unknown``.
        swept: orphaned staging and backup files (``*.hp-tmp``) removed, or
            restored from a backup, by a full index.
        skipped: entries left for a later run (temp files an operation owns
            or still being written, inside the settle time, copies owned by
            an operation, ``pending``/``overwriting``/``deleting`` copies,
            invalid paths, files changed while being indexed).
        reverified: ``pending`` copies re-checked (stale ones, and those a
            storage URL change left) and stale ``deleting`` copies retried.
        repaired: expired operations repaired.
        repair_failed: repairs that did not complete (the run fails).
        processed: files attempted within the run's budget, including repairs/removals.
        hashed_bytes: actual content bytes returned to hash readers.
    """

    listed: int = 0
    added: int = 0
    changed: int = 0
    removed: int = 0
    missing: int = 0
    unknown: int = 0
    swept: int = 0
    skipped: int = 0
    reverified: int = 0
    repaired: int = 0
    repair_failed: int = 0
    processed: int = 0
    hashed_bytes: int = 0

    def add(self, other: IndexCounts) -> None:
        """Add ``other``'s counts to these."""
        for name in IndexCounts.model_fields:
            setattr(self, name, getattr(self, name) + getattr(other, name))


class IndexSummary(IndexCounts):
    """What an index flow returns and publishes as a markdown artifact (D13).

    Attributes:
        project: project name.
        storage_location: storage location name.
        mode: ``full`` or ``incremental``.
        started_at: when the listing started.
        skipped_run: True when an incremental run found the storage location
            busy, or a stale continuation did nothing.
        remaining: True when a saved chain still has work.
    """

    project: str
    storage_location: str
    mode: IndexMode
    started_at: datetime | None = None
    skipped_run: bool = False
    remaining: bool = False


class IndexBatchResult(IndexCounts):
    """Task counts plus the last completed path and committed changed-file ids."""

    cursor_path: str | None = None
    changed_file_ids: list[int] = Field(default_factory=list)
    deferred_paths: list[str] = Field(default_factory=list)


class IndexBatchError(RuntimeError):
    """A failed task still reports work consumed, but never a resumable cursor."""

    def __init__(self, message: str, counts: IndexBatchResult) -> None:
        super().__init__(message)
        self.counts = counts

    def __reduce__(self) -> tuple[type[IndexBatchError], tuple[str, IndexBatchResult]]:
        return type(self), (str(self), self.counts)


class RemovalCandidate(BaseModel):
    """A copy :func:`index_removals` checks (serialisable for Prefect).

    Attributes:
        copy_id: the copy.
        generation: its generation when selected.
        path: the file's relative path.
    """

    copy_id: int
    generation: int
    path: str


class RemovalResult(IndexCounts):
    """Removal task counts and its last completed copy-id keyset boundary."""

    cursor_id: int = 0

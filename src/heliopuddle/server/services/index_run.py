"""The state of one index run: listing, change candidates and batch bookkeeping.

``IndexRun`` is driven by the flows in ``index.py``; it runs the
``index_batch`` task over listed entries and keeps the checkpoint
(docs/design/indexing.md).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, time, timedelta
from itertools import islice

from prefect import get_run_logger

from heliopuddle.server import models
from heliopuddle.server.context import AppContext
from heliopuddle.server.models import index as index_db
from heliopuddle.server.services.batches import BatchBudget
from heliopuddle.server.services.index_diff import NOT_DIFFED, Location
from heliopuddle.server.services.index_models import (
    IndexBatchError,
    IndexBatchResult,
    IndexEntry,
    IndexSummary,
)
from heliopuddle.server.services.index_names import IndexMode
from heliopuddle.server.services.index_tasks import index_batch
from heliopuddle.storage import StorageLocation
from heliopuddle.storage.location import TEMP_SUFFIX, ListEntry


def expand_prefixes(templates: Sequence[str], now: datetime, lookback: timedelta) -> list[str]:
    """Expand ``incremental_prefixes`` templates into the prefixes to list (D7).

    Each ``{date:<strftime>}`` template is formatted for every UTC day from
    ``now`` back to ``now - lookback`` (inclusive). Duplicates and prefixes
    inside another prefix are dropped. No templates means the whole storage
    location (``[""]``).

    Args:
        templates: e.g. ``["science/{date:%Y/%m/%d}"]``.
        now: the listing start time.
        lookback: how far back to expand.

    Returns:
        Sorted relative prefixes without leading or trailing ``/``.
    """
    if not templates:
        return [""]
    now = now.astimezone(UTC)
    day = now.date()
    last = (now - lookback).date()
    days: list[datetime] = []
    while day >= last:
        days.append(datetime.combine(day, time(), UTC))
        day -= timedelta(days=1)
    expanded = {t.format(date=d).strip("/") for t in templates for d in days}
    kept: list[str] = []
    for prefix in sorted(expanded):
        if not any(k == "" or prefix == k or prefix.startswith(k + "/") for k in kept):
            kept.append(prefix)
    return kept


def _listing(store: StorageLocation, prefixes: Sequence[str]) -> Iterator[ListEntry]:
    for prefix in sorted(prefixes, key=lambda value: tuple(value.split("/"))):
        yield from store.list_files(prefix)


def _path_order(path: str) -> tuple[tuple[int, str], ...]:
    """Files of each directory precede its name-ordered subdirectories."""
    parts = path.split("/")
    return (*((1, part) for part in parts[:-1]), (0, parts[-1]))


class IndexRun:
    def __init__(
        self, ctx: AppContext, loc: Location, mode: IndexMode, checkpoint: index_db.IndexCheckpoint
    ) -> None:
        self.ctx, self.loc, self.checkpoint = ctx, loc, checkpoint
        self.mode: IndexMode = mode
        self.store = loc.store
        self.budget = BatchBudget(loc.project.index.batch)
        self.summary = IndexSummary(
            project=loc.project.project,
            storage_location=loc.config.name,
            mode=mode,
            started_at=checkpoint.started_at,
        )
        self.failed: list[str] = []
        self.deferred: set[str] = set()
        self.prefixes = (
            expand_prefixes(
                loc.project.index.incremental_prefixes,
                checkpoint.started_at,
                loc.project.index.incremental_lookback,
            )
            if mode == "incremental"
            else [""]
        )

    def save(self) -> None:
        if not self.failed and not self.summary.repair_failed:
            with self.ctx.write_session() as session:
                index_db.save_index_checkpoint(
                    session,
                    self.loc.storage_location_id,
                    self.mode,
                    self.checkpoint,
                    expected_url=self.loc.config.identity,
                )

    def batch_size(self) -> int:
        maximum = self.budget.limits.max_files
        return min(
            self.loc.project.index.batch_size,
            max(1, maximum - self.budget.files)
            if maximum is not None
            else self.loc.project.index.batch_size,
        )

    def batch(
        self, entries: list[IndexEntry], changes: dict[str, index_db.IndexChange] | None = None
    ) -> None:
        state = index_batch(
            self.loc.project.project,
            self.loc.config.name,
            entries,
            self.mode,
            self.checkpoint.started_at,
            batch_limits=self.budget.remaining(),
            return_state=True,
        )
        if not state.is_completed():
            get_run_logger().error("index_batch from %s failed: %s", entries[0].path, state.message)
            self.failed.append(entries[0].path)
            error = state.result(raise_on_failure=False)
            if isinstance(error, IndexBatchError):
                self.budget.files += error.counts.processed
                self.budget.bytes += error.counts.hashed_bytes
            else:
                self.budget.files += len(entries)
            return
        result: IndexBatchResult = state.result()
        self.summary.add(result)
        self.summary.listed += result.processed
        self.budget.files += result.processed
        self.budget.bytes += result.hashed_bytes
        self.deferred.update(result.deferred_paths)
        if result.cursor_path is not None:
            if changes is None:
                self.checkpoint.path = result.cursor_path
            else:
                self.checkpoint.cursor = changes[result.cursor_path].cursor
        self.save()

    def prepare(
        self,
        entry: ListEntry,
        target: index_db.IndexTarget | None = None,
        *,
        changed: bool = False,
    ) -> IndexEntry | None:
        settling = (
            entry.mtime is not None
            and entry.mtime > self.checkpoint.started_at - self.loc.project.index.settle_time
        )
        is_temp = entry.path.endswith(TEMP_SUFFIX)
        if is_temp:
            return IndexEntry.from_listing(entry).model_copy(
                update={
                    "temp": self.mode == "full" and not settling,
                    "skip": self.mode != "full" or settling,
                }
            )
        if settling:
            if self.mode == "full":
                return IndexEntry.from_listing(entry, touch_only=True)
            return IndexEntry.from_listing(entry).model_copy(update={"skip": True})
        if (
            not changed
            and self.mode == "incremental"
            and self.checkpoint.since is not None
            and entry.mtime is not None
            and entry.mtime <= self.checkpoint.since
            and target is not None
            and target.copy_id is not None
        ):
            self.summary.listed += 1
            return None
        return IndexEntry.from_listing(entry)

    def changes(self, limit: int) -> list[index_db.IndexChange]:
        with self.ctx.sessionmaker() as session:
            return index_db.incremental_candidates(
                session,
                self.loc.storage_location_id,
                since=self.checkpoint.since,
                through=self.checkpoint.started_at,
                after=self.checkpoint.cursor,
                prefixes=self.prefixes,
                limit=limit,
            )

    def changed_files(self) -> bool:
        """Return True only when DB work remains after this run's budget."""
        while True:
            candidates = self.changes(self.batch_size())
            if not candidates:
                self.checkpoint.path = ""
                return False
            if self.budget.exhausted:
                return True
            entries: list[IndexEntry] = []
            for candidate in candidates:
                try:
                    info = self.store.info(candidate.path)
                except FileNotFoundError:
                    entries.append(IndexEntry(path=candidate.path, size=0, skip=True))
                    continue
                entry = self.prepare(ListEntry(candidate.path, info.size, info.mtime), changed=True)
                assert entry is not None
                entries.append(entry)
            self.batch(entries, {candidate.path: candidate for candidate in candidates})
            if self.failed:
                return False

    def unseen(
        self, entry: ListEntry, target: index_db.IndexTarget | None, *, late: bool = False
    ) -> bool:
        if entry.path in self.deferred:
            return False
        if (
            target is not None
            and target.last_seen_at is not None
            and target.last_seen_at >= self.checkpoint.started_at
            and target.copy_size == entry.size
            and target.copy_mtime == entry.mtime
        ):
            return False
        if late:
            if entry.path.endswith(TEMP_SUFFIX) or (
                entry.mtime is not None
                and entry.mtime > self.checkpoint.started_at - self.loc.project.index.settle_time
            ):
                return False
            try:
                models.validate_path(entry.path)
            except ValueError:
                return False
            if target is not None and (
                target.op_token is not None
                or (target.copy_state in NOT_DIFFED and not target.awaits_recheck)
            ):
                return False
            if (
                self.mode == "incremental"
                and self.checkpoint.since is not None
                and entry.mtime is not None
                and entry.mtime <= self.checkpoint.since
                and target is not None
                and target.copy_id is not None
            ):
                return False
        return True

    def entries(self, cursor: str = "") -> Iterator[tuple[ListEntry, index_db.IndexTarget | None]]:
        """Join streamed listing pages to DB observations without per-path queries."""
        listing = (
            entry
            for entry in _listing(self.store, self.prefixes)
            if not cursor or _path_order(entry.path) > _path_order(cursor)
        )
        while chunk := list(islice(listing, 1000)):
            with self.ctx.sessionmaker() as session:
                targets = index_db.get_index_targets(
                    session,
                    self.loc.project_id,
                    self.loc.storage_location_id,
                    [entry.path for entry in chunk],
                )
            for entry in chunk:
                yield entry, targets.get(entry.path)

    def listing(self) -> bool:
        """Stream files then directories, stopping at an actual unfinished entry."""
        batch: list[IndexEntry] = []
        cursor = self.checkpoint.path or ""
        for entry, target in self.entries(cursor):
            if not self.unseen(entry, target):
                continue
            prepared = self.prepare(entry, target)
            if prepared is None:
                continue
            if self.budget.exhausted:
                return True
            batch.append(prepared)
            if len(batch) >= self.batch_size():
                self.batch(batch)
                batch = []
        if batch:
            self.batch(batch)
        if self.failed:
            return False
        # A concurrent writer can insert a path before the cursor, even when
        # there is no DB row for it yet. Re-walk lazily; never sort/materialise.
        if any(self.unseen(entry, target, late=True) for entry, target in self.entries()):
            self.checkpoint.path = ""
            return True
        return False

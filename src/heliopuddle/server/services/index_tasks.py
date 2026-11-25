"""The ``index_batch`` and ``index_removals`` tasks (SVC-03, SVC-08, SVC-17).

Per-batch work of the index flows in ``index.py``; the diff rules live in
``index_diff.py`` and are documented in docs/design/indexing.md.
"""

from __future__ import annotations

from datetime import datetime

from prefect import get_run_logger, task
from prefect.cache_policies import NO_CACHE
from sqlalchemy import select

from heliopuddle.config.models import BatchConfig
from heliopuddle.server.context import AppContext, get_context
from heliopuddle.server.database.orm import File, utcnow
from heliopuddle.server.models import index as index_db
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services.batches import BatchBudget
from heliopuddle.server.services.index_diff import (
    NOT_DIFFED,
    HashingStore,
    count_outcome,
    index_known,
    index_new,
    index_recheck,
    resolve_location,
)
from heliopuddle.server.services.index_models import (
    IndexBatchError,
    IndexBatchResult,
    IndexEntry,
    RemovalCandidate,
    RemovalResult,
)
from heliopuddle.server.services.index_names import SYSTEM_USER, IndexMode
from heliopuddle.server.services.index_sweep import sweep_temp_file
from heliopuddle.storage import StorageLocation
from heliopuddle.storage.credentials import open_location


class _BatchRun:
    """The state of one ``index_batch`` task run."""

    def __init__(
        self,
        ctx: AppContext,
        project: str,
        storage_location: str,
        listed_at: datetime | None,
        batch_limits: BatchConfig | None,
    ) -> None:
        self.ctx = ctx
        self.project, self.storage_location = project, storage_location
        self.loc = resolve_location(ctx, project, storage_location)
        self.store = open_location(self.loc.config, HashingStore)
        self.seen_at = listed_at or utcnow()
        self.now = utcnow()
        self.counts = IndexBatchResult()
        self.budget = BatchBudget(batch_limits) if batch_limits is not None else None
        self.changed_paths: list[str] = []
        # Every listed copy is recorded as seen (set-free removal detection,
        # SVC-17); a copy changed by this batch has a new generation, so the
        # touch leaves it alone.
        self.seen: list[tuple[int, int]] = []
        self.failed: list[str] = []
        self.targets: dict[str, index_db.IndexTarget] = {}
        self.logger = get_run_logger()

    def run(self, entries: list[IndexEntry]) -> IndexBatchResult:
        """Diff every entry, then record sightings and notify rules."""
        counts = self.counts
        with self.ctx.sessionmaker() as session:
            self.targets = index_db.get_index_targets(
                session,
                self.loc.project_id,
                self.loc.storage_location_id,
                [e.path for e in entries],
            )
        for entry in entries:
            if self._exhausted():
                break
            counts.processed += 1
            counts.cursor_path = entry.path
            self._step(entry)
        if self.seen:
            with self.ctx.write_session() as session:
                index_db.touch_copies(session, self.seen, self.seen_at)
        counts.hashed_bytes = self.store.bytes_read
        if self.changed_paths:
            counts.changed_file_ids = self._changed_file_ids()
        if counts.changed_file_ids:
            self._notify_rules()
        self._log(len(entries))
        if self.failed:
            raise IndexBatchError(f"indexing failed for: {', '.join(self.failed)}", counts)
        return counts

    def _exhausted(self) -> bool:
        if self.budget is None:
            return False
        self.budget.files, self.budget.bytes = self.counts.processed, self.store.bytes_read
        return self.budget.exhausted

    def _step(self, entry: IndexEntry) -> None:
        counts = self.counts
        if entry.skip:
            counts.skipped += 1
            return
        if entry.touch_only:
            counts.skipped += 1
        if entry.temp:
            self._sweep(entry)
            return
        target = self.targets.get(entry.path)
        if target is not None and target.copy_id is not None and target.generation is not None:
            self.seen.append((target.copy_id, target.generation))
            if entry.touch_only or self._leave_alone(entry, target):
                return
        if entry.touch_only:
            return
        self._diff(entry, target)

    def _leave_alone(self, entry: IndexEntry, target: index_db.IndexTarget) -> bool:
        """True if the copy needs no diff: owned, not diffed, or unchanged."""
        if target.is_owned(self.now) or (
            target.copy_state in NOT_DIFFED and not target.awaits_recheck
        ):
            self.counts.skipped += 1
            return True
        return (
            target.copy_state in (FileState.AVAILABLE, FileState.UNKNOWN)
            and target.copy_size == entry.size
            and target.copy_mtime == entry.mtime
        )

    def _sweep(self, entry: IndexEntry) -> None:
        try:
            swept = sweep_temp_file(
                self.ctx,
                self.store,
                project_id=self.loc.project_id,
                storage_location_id=self.loc.storage_location_id,
                path=entry.path,
            )
        except Exception:
            self.logger.exception("index: sweeping %s failed", entry.path)
            self.failed.append(entry.path)
            return
        if swept == "left":
            self.counts.skipped += 1
        else:
            self.counts.swept += 1

    def _diff(self, entry: IndexEntry, target: index_db.IndexTarget | None) -> None:
        args = (self.ctx, self.loc, self.store, entry)
        try:
            if target is None:
                outcome = index_new(*args, self.seen_at)
            elif target.awaits_recheck:
                outcome = index_recheck(*args, target, self.seen_at)
            else:
                outcome = index_known(*args, target, self.seen_at)
        except Exception:
            self.logger.exception(
                "index: %s in %s/%s failed", entry.path, self.project, self.storage_location
            )
            self.failed.append(entry.path)
            return
        count_outcome(self.counts, outcome)
        if outcome == "skipped":
            self.counts.deferred_paths.append(entry.path)
        else:
            self.changed_paths.append(entry.path)

    def _changed_file_ids(self) -> list[int]:
        with self.ctx.sessionmaker() as session:
            changed = select(File.id).where(
                File.project_id == self.loc.project_id,
                File.path.in_(self.changed_paths),
                File.deleted_at.is_(None),
            )
            series = select(File.series_id).where(File.id.in_(changed))
            return list(
                session.scalars(
                    select(File.id)
                    .where(
                        (File.id.in_(changed))
                        | (File.series_id.in_(series) & (File.updated_at >= self.now))
                    )
                    .order_by(File.id)
                )
            )

    def _notify_rules(self) -> None:
        from heliopuddle.server.services.rule_control import notify_file_changes

        try:
            notify_file_changes(self.ctx, self.project, self.counts.changed_file_ids)
        except Exception:
            self.logger.exception(
                "index: notifying rules of committed changes in %s failed", self.project
            )

    def _log(self, batch_size: int) -> None:
        counts = self.counts
        if counts.added or counts.changed or counts.unknown or counts.swept or self.failed:
            self.logger.info(
                "index %s/%s: batch of %d: %d added, %d updated, %d unknown, %d swept, %d failed",
                self.project,
                self.storage_location,
                batch_size,
                counts.added,
                counts.changed,
                counts.unknown,
                counts.swept,
                len(self.failed),
            )


@task(
    name="index_batch",
    task_run_name="index_batch-{mode}-{storage_location}",
    cache_policy=NO_CACHE,
)
def index_batch(
    project: str,
    storage_location: str,
    entries: list[IndexEntry],
    mode: IndexMode = "full",
    listed_at: datetime | None = None,
    *,
    batch_limits: BatchConfig | None = None,
) -> IndexBatchResult:
    """Apply the index diff rules to listed entries (see docs/design/indexing.md).

    For each entry, compared with the DB:

    - no live file at the path: added (series and version from the path
      rules, md5, ``created_by`` ``heliopuddle:index``) with an ``available``
      copy, or ``unknown`` when the storage location does not accept external
      changes (or the version rules give no valid version);
    - a live file with no copy here: a copy is added, ``available`` if the
      hash matches and external changes are accepted, else ``unknown``;
    - same size and mtime: ``last_seen_at`` is updated;
    - size or mtime changed (or the copy was ``missing``): with external
      changes accepted, re-hashed and the file takes the new hash and size
      (copy ``available``); otherwise the copy becomes ``unknown``;
    - a ``pending`` copy never observed in this root (after a storage URL
      change): re-checked against the kept hash and size;
    - copies owned by an operation, other ``pending`` copies, and
      ``overwriting`` or ``deleting`` copies are skipped;
    - a ``*.hp-tmp`` entry (full index only) that no operation owns is swept
      (:func:`heliopuddle.server.services.index_sweep.sweep_temp_file`): a
      staged file is removed, a backup is restored when the final object is
      missing or differs from the file's hash, else removed.

    Each object is observed once (stat, hash, stat); one that changes
    meanwhile is skipped. Each change expects the copy's generation as read;
    a copy changed meanwhile is skipped. Entries marked ``touch_only`` only
    get ``last_seen_at``. Every entry is attempted; if any fails, the task
    fails afterwards naming the paths. Committed changes notify rules after
    the transactions close, even when another entry failed. Notification
    failures are logged without failing indexing.

    Args:
        project: project name.
        storage_location: storage location name.
        entries: listed files (temp files only in a full index, marked ``temp``).
        mode: the calling flow's mode (shown in the task run name).
        listed_at: when the listing started; recorded as ``last_seen_at``.

    Returns:
        Counts, the last completed path and committed changed-file ids.
        ``listed`` is counted by the flow.

    Raises:
        LookupError: if the storage location is not in config.
        RuntimeError: if indexing any entry failed.
    """
    ctx = get_context()
    return _BatchRun(ctx, project, storage_location, listed_at, batch_limits).run(entries)


def _still_in_store(store: StorageLocation, path: str) -> bool:
    """True if ``path`` exists; only "not found" means gone (other errors are raised).

    ``fs.exists`` answers False on any error, which must never count as a removal.
    """
    try:
        store.info(path)
    except FileNotFoundError:
        return False
    return True


@task(
    name="index_removals", task_run_name="index_removals-{storage_location}", cache_policy=NO_CACHE
)
def index_removals(
    project: str,
    storage_location: str,
    candidates: list[RemovalCandidate],
    listed_at: datetime,
    *,
    batch_limits: BatchConfig | None = None,
) -> RemovalResult:
    """Record copies a full index did not find in the store.

    Each copy is checked again in the store (outside any transaction): if it
    exists after all it is left alone (skipped). Otherwise, with external
    changes accepted, the copy becomes ``deleted`` and the file is
    soft-deleted when it has no other copy; else the copy becomes ``missing``.
    The change expects the copy's generation as selected; a copy changed
    since, or since the listing started, is skipped. ``is_latest`` is
    recomputed.

    Args:
        project: project name.
        storage_location: storage location name.
        candidates: copies absent from the listing.
        listed_at: when the listing started.

    Returns:
        The ``removed``, ``missing`` and ``skipped`` counts.
    """
    ctx = get_context()
    loc = resolve_location(ctx, project, storage_location)
    store = loc.store
    counts = RemovalResult()
    budget = BatchBudget(batch_limits) if batch_limits is not None else None
    for candidate in candidates:
        if budget is not None:
            budget.files = counts.processed
            if budget.exhausted:
                break
        counts.processed += 1
        counts.cursor_id = candidate.copy_id
        if _still_in_store(store, candidate.path):
            counts.skipped += 1
            continue
        with ctx.write_session() as session:
            outcome = index_db.index_remove_copy(
                session,
                candidate.copy_id,
                before=listed_at,
                accept=loc.config.accept_external_changes,
                user=SYSTEM_USER,
                generation=candidate.generation,
            )
        if outcome == "removed":
            counts.removed += 1
        elif outcome == "missing":
            counts.missing += 1
        else:
            counts.skipped += 1
    if counts.removed or counts.missing:
        get_run_logger().info(
            "index %s/%s: %d cop(ies) removed, %d marked missing (not found in the store)",
            project,
            storage_location,
            counts.removed,
            counts.missing,
        )
    return counts


# --- listing --------------------------------------------------------------------

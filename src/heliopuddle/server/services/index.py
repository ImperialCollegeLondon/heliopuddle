"""Index flows: keep the DB in step with each storage location (SVC-03, SVC-08, SVC-10).

``index_full`` and ``index_incremental`` stream a listing of a storage
location and run the ``index_batch`` task over every ``index.batch_size``
listed entries (D1). The task applies the diff rules in
docs/design/indexing.md: each object is observed once (stat, hash, stat)
outside any transaction, and each file's DB change is one guarded write
transaction that expects the copy's generation (docs/design/operations.md).
Copies owned by another operation are skipped. Only the full index detects
removals (set-free: listed copies get ``last_seen_at`` = the listing start)
and repairs expired operations and stale ``pending``/``deleting`` copies.
"""

from __future__ import annotations

import re
from datetime import datetime
from time import sleep
from typing import Any

from prefect import flow, get_run_logger
from prefect.artifacts import create_markdown_artifact

from heliopuddle.server.context import AppContext, get_context
from heliopuddle.server.database.engine import refresh_statistics
from heliopuddle.server.database.orm import utcnow
from heliopuddle.server.models import index as index_db
from heliopuddle.server.services.concurrency import (
    ConcurrencyBusyError,
    ensure_concurrency_limit,
    hp_concurrency,
)
from heliopuddle.server.services.index_diff import Location, Observation, resolve_location
from heliopuddle.server.services.index_finish import finish_full
from heliopuddle.server.services.index_models import (
    IndexBatchError,
    IndexBatchResult,
    IndexCounts,
    IndexEntry,
    IndexSummary,
    RemovalCandidate,
    RemovalResult,
)
from heliopuddle.server.services.index_names import (
    IndexMode,
    index_deployment_full_name,
    storage_location_lock,
)
from heliopuddle.server.services.index_run import IndexRun, expand_prefixes
from heliopuddle.server.services.index_tasks import index_batch, index_removals

__all__ = [
    "IndexBatchError",
    "IndexBatchResult",
    "IndexCounts",
    "IndexEntry",
    "IndexSummary",
    "Observation",
    "RemovalCandidate",
    "RemovalResult",
    "expand_prefixes",
    "index_batch",
    "index_full",
    "index_full_chain",
    "index_incremental",
    "index_removals",
]


def _artifact_key(mode: str, project: str, storage_location: str) -> str:
    key = re.sub(r"[^a-z0-9-]+", "-", f"index-{mode}-{project}-{storage_location}".lower())
    return re.sub(r"-{2,}", "-", key).strip("-")


def _log_summary(summary: IndexSummary) -> None:
    if summary.skipped_run:
        return
    get_run_logger().info(
        "index %s of %s/%s %s: %d listed, %d added, %d updated, %d removed, %d missing, "
        "%d unknown, %d repaired, %d re-verified, %d skipped, %d swept (%d bytes hashed)",
        summary.mode,
        summary.project,
        summary.storage_location,
        "has work remaining" if summary.remaining else "finished",
        summary.listed,
        summary.added,
        summary.changed,
        summary.removed,
        summary.missing,
        summary.unknown,
        summary.repaired,
        summary.reverified,
        summary.skipped,
        summary.swept,
        summary.hashed_bytes,
    )


def _publish(summary: IndexSummary) -> None:
    rows = "\n".join(f"| {name} | {getattr(summary, name)} |" for name in IndexCounts.model_fields)
    status = (
        "skipped: busy storage location or stale continuation"
        if summary.skipped_run
        else "batch completed; work remains"
        if summary.remaining
        else "completed"
    )
    markdown = (
        f"# Index {summary.mode}: {summary.project}/{summary.storage_location}\n\n"
        f"Status: {status}. Listing started: {summary.started_at or '-'}.\n\n"
        f"| count | value |\n|---|---|\n{rows}\n"
    )
    create_markdown_artifact(
        markdown=markdown,
        key=_artifact_key(summary.mode, summary.project, summary.storage_location),
        description=f"index {summary.mode} summary",
    )


def _run(
    mode: IndexMode,
    project: str,
    storage_location: str,
    since: datetime | None,
    continuation: dict[str, Any] | None = None,
    *,
    retrigger: bool = True,
) -> IndexSummary:
    ctx = get_context()
    loc = resolve_location(ctx, project, storage_location)
    lock = storage_location_lock(project, storage_location)
    ensure_concurrency_limit(lock)
    timeout = loc.project.index.lock_timeout.total_seconds() if mode == "full" else None
    try:
        with hp_concurrency(lock, timeout_seconds=timeout, wait=mode == "full"):
            summary = _index(ctx, loc, mode, since, continuation, retrigger=retrigger)
    except ConcurrencyBusyError:
        get_run_logger().info("index: %s is busy; skipping this incremental run", lock)
        summary = IndexSummary(
            project=project, storage_location=storage_location, mode=mode, skipped_run=True
        )
    _log_summary(summary)
    _publish(summary)
    return summary


def _checkpoint_parameters(checkpoint: index_db.IndexCheckpoint) -> dict[str, Any]:
    return {
        "started_at": checkpoint.started_at.isoformat(),
        "since": checkpoint.since.isoformat() if checkpoint.since is not None else None,
        "path": checkpoint.path,
        "cursor": checkpoint.cursor.model_dump(mode="json") if checkpoint.cursor else None,
    }


def _schedule_successor(loc: Location, mode: IndexMode, *, parameters: dict[str, Any]) -> None:
    from hashlib import sha256

    from prefect.deployments import run_deployment

    continuation = parameters["continuation"]
    key = sha256(repr(continuation).encode()).hexdigest()
    run_deployment(
        index_deployment_full_name(
            mode, loc.project_id, loc.storage_location_id, loc.project.project, loc.config.name
        ),
        parameters=parameters,
        scheduled_time=utcnow() + loc.project.index.pause_between_runs,
        idempotency_key=f"index-{mode}-sl{loc.storage_location_id}-{key}",
        timeout=0,
        as_subflow=False,
    )
    get_run_logger().info(
        "index %s of %s/%s has work remaining; scheduled a continuation in %s",
        mode,
        loc.project.project,
        loc.config.name,
        loc.project.index.pause_between_runs,
    )


def _index(
    ctx: AppContext,
    loc: Location,
    mode: IndexMode,
    since: datetime | None,
    continuation: dict[str, Any] | None = None,
    *,
    retrigger: bool = True,
) -> IndexSummary:
    with ctx.sessionmaker() as session:
        checkpoint = index_db.index_checkpoint(session, loc.storage_location_id, mode)
        progress = index_db.get_index_progress(session, loc.project.project, loc.config.name)
        if continuation is not None and (
            checkpoint is None or continuation["started_at"] != checkpoint.started_at.isoformat()
        ):
            return IndexSummary(
                project=loc.project.project,
                storage_location=loc.config.name,
                mode=mode,
                skipped_run=True,
            )
        if checkpoint is None or since is not None:
            lower = (
                since
                if since is not None
                else (progress.last_incremental_at if progress is not None else None)
            )
            checkpoint = index_db.IndexCheckpoint(
                utcnow(), lower - loc.project.index.incremental_overlap if lower else None
            )
    run = IndexRun(ctx, loc, mode, checkpoint)
    remaining = False
    if mode == "incremental" and checkpoint.path is None:
        remaining = run.changed_files()
    if not remaining and not run.failed and not (checkpoint.path or "").startswith("//"):
        remaining = run.listing()
    if mode == "full" and not remaining and not run.failed:
        remaining = finish_full(run)
    if run.summary.repair_failed:
        run.failed.append(f"{run.summary.repair_failed} repair(s) incomplete")
    if run.failed:
        raise RuntimeError(
            f"index {mode} of {loc.project.project}/{loc.config.name} failed "
            f"({'; '.join(run.failed)})"
        )
    run.summary.remaining = remaining
    if remaining:
        run.save()
        if retrigger and loc.project.index.retrigger:
            _schedule_successor(
                loc,
                mode,
                parameters={
                    "project": loc.project.project,
                    "storage_location": loc.config.name,
                    "continuation": _checkpoint_parameters(checkpoint),
                },
            )
    else:
        watermark = checkpoint.started_at
        if mode == "incremental":
            watermark -= loc.project.index.settle_time
        with ctx.write_session() as session:
            index_db.record_index_progress(
                session,
                loc.storage_location_id,
                mode,
                watermark,
                expected_url=loc.config.display_url,
            )
        if mode == "full":
            # A finished full chain may have added many rows: let SQLite plan with them.
            refresh_statistics(ctx.engine)
    return run.summary


@flow(name="index_full")
def index_full(
    project: str, storage_location: str, *, continuation: dict[str, Any] | None = None
) -> IndexSummary:
    """Index a whole storage location: adds, changes, removals and repair (SVC-03, SVC-10).

    Holds the storage location's concurrency limit, waiting up to
    ``index.lock_timeout``. Streams a listing of the whole storage location,
    runs :func:`index_batch` per ``index.batch_size`` entries, repairs
    expired operations, records copies not seen by the listing
    (:func:`index_removals`), re-verifies stale ``pending`` copies and
    retries stale ``deleting`` ones. On success,
    ``index_progress.last_full_at`` is set to the listing start time. The
    summary is published as a markdown artifact.

    Args:
        project: project name.
        storage_location: storage location name.

    Returns:
        The run summary.

    Raises:
        LookupError: if the storage location is not in config.
        TimeoutError: if the lock was not acquired within ``index.lock_timeout``.
        RuntimeError: if any batch or removal failed or a repair did not
            complete (earlier completed batches keep their checkpoints).
    """
    return _run("full", project, storage_location, None, continuation)


def index_full_chain(project: str, storage_location: str) -> list[IndexSummary]:
    """Run a whole ``index_full`` chain inside the calling flow, batch by batch (reconcile).

    Each batch is the same bounded batch an ``index_full`` run does, saving
    its checkpoint; instead of scheduling a successor run, the next batch
    runs here after ``index.pause_between_runs``. See
    docs/design/rules.md#reconcile-m5.

    Args:
        project: project name.
        storage_location: storage location name.

    Returns:
        One summary per batch, the last with ``remaining`` False.

    Raises:
        RuntimeError: as :func:`index_full`, if a batch, removal or repair failed.
    """
    ctx = get_context()
    loc = resolve_location(ctx, project, storage_location)
    pause = loc.project.index.pause_between_runs.total_seconds()
    summaries: list[IndexSummary] = []
    continuation: dict[str, Any] | None = None
    while True:
        summary = _run("full", project, storage_location, None, continuation, retrigger=False)
        summaries.append(summary)
        if not summary.remaining:
            return summaries
        with ctx.sessionmaker() as session:
            checkpoint = index_db.index_checkpoint(session, loc.storage_location_id, "full")
        if checkpoint is None:
            # The storage-location lock was released after the batch: another
            # full-index run took the chain over and finished it meanwhile.
            get_run_logger().info(
                "index: %s/%s full chain was finished by another run", project, storage_location
            )
            return summaries
        continuation = _checkpoint_parameters(checkpoint)
        if pause:
            sleep(pause)


@flow(name="index_incremental")
def index_incremental(
    project: str,
    storage_location: str,
    since: datetime | None = None,
    *,
    continuation: dict[str, Any] | None = None,
) -> IndexSummary:
    """Index files changed since the last incremental run (SVC-08, SVC-14).

    Files with an mtime after ``since`` (default: the
    ``index_progress.last_incremental_at`` watermark; none means every file)
    minus ``index.incremental_overlap`` are processed, under the expanded
    ``index.incremental_prefixes`` when set. Changed DB files/copies and
    previously unindexed store paths are also considered, independently of
    mtime. Removals are not detected. If
    the storage location's concurrency limit is busy, the run does nothing and
    returns a summary with ``skipped_run``. On success the watermark becomes
    the listing start time minus ``index.settle_time``, so files skipped while
    settling are processed by a later run.

    Args:
        project: project name.
        storage_location: storage location name.
        since: overrides the stored watermark.

    Returns:
        The run summary.

    Raises:
        LookupError: if the storage location is not in config.
        RuntimeError: if any batch failed (the watermark is not advanced).
    """
    return _run("incremental", project, storage_location, since, continuation)

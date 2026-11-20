"""The tail of a full index: removals, expired-operation repair, stale copies.

Run only after the listing chain finishes, in bounded keyset phases that save
their position in the checkpoint (docs/design/indexing.md).
"""

from __future__ import annotations

import importlib
from datetime import datetime
from typing import Any, Literal

from prefect import get_run_logger

from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import utcnow
from heliopuddle.server.models import index as index_db
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services.delete import delete_file
from heliopuddle.server.services.index_models import RemovalCandidate, RemovalResult
from heliopuddle.server.services.index_names import SYSTEM_USER
from heliopuddle.server.services.index_run import IndexRun
from heliopuddle.server.services.index_tasks import index_removals
from heliopuddle.server.services.verify import verify_file
from heliopuddle.storage.hashing import count_hash_reads

REMOVAL_PAGE_SIZE = 1000
"""Removal candidates read per query (keyset pages; SVC-17)."""


RepairStatus = Literal["not_owned_or_live", "repaired", "dropped", "incomplete"]


def _find_expired(ctx: AppContext, storage_location_id: int, now: datetime) -> list[int]:
    """Copies of the storage location whose operation expired (``uploads.find_expired``)."""
    uploads: Any = importlib.import_module("heliopuddle.server.models.uploads")
    with ctx.sessionmaker() as session:
        return [int(i) for i in uploads.find_expired(session, storage_location_id, now)]


def _repair_expired(copy_id: int, now: datetime) -> str:
    """Repair one expired operation (``upload.repair_expired``) and return its status."""
    upload: Any = importlib.import_module("heliopuddle.server.services.upload")
    result = upload.repair_expired(copy_id, now=now)
    status = getattr(result, "status", result)
    return str(getattr(status, "value", status))


# --- flows ----------------------------------------------------------------------


def finish_full(run: IndexRun) -> bool:
    """Repair/remove in bounded keyset phases, only after the listing chain finishes."""
    ctx, loc, checkpoint = run.ctx, run.loc, run.checkpoint
    phase, after = "expired", 0
    if (checkpoint.path or "").startswith("//"):
        phase, value = (checkpoint.path or "")[2:].split("/", 1)
        after = int(value)
    phases = ("expired", "removals", "pending", "deleting")
    for current in phases[phases.index(phase) :]:
        after = after if current == phase else 0
        if current == "removals":
            while True:
                with ctx.sessionmaker() as session:
                    page = index_db.removal_candidates(
                        session,
                        loc.storage_location_id,
                        checkpoint.started_at,
                        after_id=after,
                        limit=min(REMOVAL_PAGE_SIZE, run.batch_size()),
                    )
                if not page:
                    break
                if run.budget.exhausted:
                    checkpoint.path = f"//{current}/{after}"
                    return True
                state = index_removals(
                    loc.project.project,
                    loc.config.name,
                    [
                        RemovalCandidate(copy_id=c.copy_id, generation=c.generation, path=c.path)
                        for c in page
                    ],
                    checkpoint.started_at,
                    batch_limits=run.budget.remaining(),
                    return_state=True,
                )
                if not state.is_completed():
                    run.failed.append(f"removals {page[0].copy_id}: {state.message}")
                    return False
                result: RemovalResult = state.result()
                run.summary.add(result)
                run.budget.files += result.processed
                after = result.cursor_id
                checkpoint.path = f"//{current}/{after}"
                run.save()
            continue
        now = utcnow()
        if current == "expired":
            candidates = [(id, 0) for id in _find_expired(ctx, loc.storage_location_id, now)]
        else:
            with ctx.sessionmaker() as session:
                candidates = index_db.stale_copies(
                    session,
                    loc.storage_location_id,
                    FileState.PENDING if current == "pending" else FileState.DELETING,
                    checkpoint.started_at - loc.project.index.reverify_after,
                )
        for copy_id, file_id in candidates:
            if copy_id <= after:
                continue
            if run.budget.exhausted:
                checkpoint.path = f"//{current}/{after}"
                return True
            with count_hash_reads() as reads:
                _repair_one(run, current, copy_id, file_id, now)
            run.budget.bytes += reads.bytes
            run.summary.hashed_bytes += reads.bytes
            run.budget.files += 1
            run.summary.processed += 1
            after = copy_id
            checkpoint.path = f"//{current}/{after}"
            run.save()
    return False


def _repair_one(run: IndexRun, phase: str, copy_id: int, file_id: int, now: datetime) -> None:
    summary, ctx = run.summary, run.ctx
    if phase == "expired":
        try:
            status = _repair_expired(copy_id, now)
        except Exception:
            get_run_logger().exception("index: repairing copy %s failed", copy_id)
            status = "incomplete"
        if status in ("repaired", "dropped"):
            summary.repaired += 1
        elif status == "not_owned_or_live":
            summary.skipped += 1
        else:
            summary.repair_failed += 1
    elif phase == "pending":
        state = verify_file(copy_id, return_state=True)
        result = state.result(raise_on_failure=False) if state.is_completed() else None
        if result in (FileState.AVAILABLE, FileState.UNKNOWN) or not _still_awaits(
            ctx, copy_id, FileState.PENDING
        ):
            summary.reverified += 1
        else:
            summary.repair_failed += 1
    else:
        state = delete_file(file_id, SYSTEM_USER, return_state=True)
        result = state.result(raise_on_failure=False) if state.is_completed() else None
        if result == FileState.DELETED:
            summary.reverified += 1
        else:
            summary.repair_failed += 1


def _still_awaits(ctx: AppContext, copy_id: int, state: FileState) -> bool:
    with ctx.sessionmaker() as session:
        return index_db.copy_awaits_repair(session, copy_id, state)

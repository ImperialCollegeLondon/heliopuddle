"""Repair an expired upload, overwrite or delete (STO-10)."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from importlib import import_module

from heliopuddle.server.context import AppContext, get_context
from heliopuddle.server.models import transitions as tr
from heliopuddle.server.models import uploads
from heliopuddle.server.models.snapshots import CopySnapshot
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services.delete import delete_copies
from heliopuddle.server.services.run_logging import run_logger
from heliopuddle.server.services.stores import observe, store_for
from heliopuddle.server.services.upload.complete import _abandon
from heliopuddle.server.services.upload.objects import temp_files
from heliopuddle.server.services.upload.results import (
    _TO_REPAIR,
    REPAIR_USER,
    RepairResult,
    _Settled,
)
from heliopuddle.server.services.upload.settle_new import _settle_new, _settle_staged_new
from heliopuddle.server.services.upload.settle_overwrite import _settle_overwrite
from heliopuddle.storage import StorageLocation

logger = run_logger("heliopuddle.server.services.upload")


def _notify_file_changes(ctx: AppContext, project: str, file_ids: Iterable[int]) -> None:
    """Load the rule-control boundary only when a committed repair needs dispatch."""
    notify = import_module("heliopuddle.server.services.rule_control").notify_file_changes
    notify(ctx, project, file_ids)


def _settle_staged(
    ctx: AppContext, store: StorageLocation, snap: CopySnapshot, token: str | None
) -> _Settled:
    """Settle an ``overwriting`` copy or a staged ``pending`` upload from its temporary files."""
    try:
        staged, backups = temp_files(store, snap.path)
    except Exception:
        logger.exception("repair of copy %s: listing temporary files failed", snap.copy_id)
        return _Settled.INCOMPLETE
    if snap.state == FileState.OVERWRITING:
        return _settle_overwrite(ctx, store, snap, staged, backups, published_token=token)
    return _settle_staged_new(ctx, store, snap, staged, backups, published_token=token)


def _repair_snapshot(
    ctx: AppContext, snap: CopySnapshot, expired_token: str | None, store: StorageLocation
) -> RepairResult:
    """Settle a taken-over copy according to its state."""
    if snap.state == FileState.OVERWRITING or (snap.state == FileState.PENDING and snap.staged):
        return _TO_REPAIR[_settle_staged(ctx, store, snap, expired_token)]
    if snap.state == FileState.PENDING:
        return _TO_REPAIR[_settle_new(ctx, store, snap, published_token=expired_token)]
    return RepairResult.INCOMPLETE  # pragma: no cover - only pending, overwriting, deleting


def repair_expired(copy_id: int, *, now: datetime | None = None) -> RepairResult:
    """Repair a copy whose operation expired, or whose delete did not finish.

    The copy is taken over (a fresh token), then settled from what the store
    holds:

    - ``pending`` (new-file upload): the final object is verified if it
      matches the reserved content; otherwise a reservation that created the
      copy is dropped with its object, and one on an existing copy leaves the
      object and marks the copy ``unknown`` (``missing`` without one);
    - ``overwriting``: the final object, a backup and the staged files
      (``<path>.*.hp-tmp``) decide: new content published, or original kept
      (a backup is restored when the final object is missing or wrong); the
      temporary files are removed;
    - ``deleting``: the guarded delete of that copy is run again.

    Never raises on store or config errors: they give ``incomplete`` and the
    copy is left for the next repair.

    Args:
        copy_id: the copy (e.g. from :func:`heliopuddle.server.models.uploads.find_expired`).
        now: the time expiry is judged at (default: now).

    Returns:
        What was done.
    """
    ctx = get_context()
    now = now or tr.clock()
    try:
        with ctx.write_session() as session:
            taken = uploads.take_over_expired(session, copy_id, now, ctx.settings.op_lease)
    except Exception:
        logger.exception("repair of copy %s: taking it over failed", copy_id)
        return RepairResult.INCOMPLETE
    if taken is None:
        return RepairResult.NOT_OWNED_OR_LIVE
    snap, expired_token = taken
    logger.info("repairing expired copy %s (%s, %s)", copy_id, snap.state.value, snap.path)
    try:
        store = store_for(ctx, snap.project, snap.storage_location)
    except LookupError as e:
        logger.error("repair of copy %s: %s", copy_id, e)
        _abandon(ctx, snap)
        return RepairResult.INCOMPLETE
    if snap.state == FileState.DELETING:
        result = _repair_delete(ctx, snap)
    else:
        result = _repair_snapshot(ctx, snap, expired_token, store)
    logger.info(
        "repair of copy %s (%s/%s in %s): %s",
        copy_id,
        snap.project,
        snap.path,
        snap.storage_location,
        result.value,
    )
    if result == RepairResult.INCOMPLETE:
        _abandon(ctx, snap)
    elif result in {RepairResult.REPAIRED, RepairResult.DROPPED}:
        try:
            _notify_file_changes(ctx, snap.project, [snap.file_id])
        except Exception:
            logger.exception("rule notification failed after repairing copy %s", snap.copy_id)
    return result


def _repair_delete(ctx: AppContext, snap: CopySnapshot) -> RepairResult:
    from heliopuddle.server import models

    if snap.move_destination_id is not None:
        return _repair_move_delete(ctx, snap)
    outcomes = delete_copies(ctx, snap.project, [snap], verify_hash=True)
    try:
        with ctx.write_session() as session:
            models.finish_delete(session, snap.file_id, [snap], outcomes, updated_by=REPAIR_USER)
    except Exception:
        logger.exception("repair of copy %s: recording the delete failed", snap.copy_id)
        return RepairResult.INCOMPLETE
    if outcomes[snap.copy_id] == FileState.DELETING:
        return RepairResult.INCOMPLETE
    return RepairResult.REPAIRED


def _repair_move_delete(ctx: AppContext, snap: CopySnapshot) -> RepairResult:
    """Retry a move's interrupted source delete only while its destination holds the content.

    The destination copy recorded when the move began deleting is re-checked
    in the DB and read back from its store immediately before ``rm``, as the
    move itself did. If it no longer holds the moved content, the source is
    settled without deleting (token-less ``pending``, re-checked by the next
    full index), so a repair never removes the last valid copy.
    """
    from heliopuddle.server import models
    from heliopuddle.server.models import transfers
    from heliopuddle.server.services.delete import delete_object

    class _DestinationLost(Exception):
        pass

    def destination_holds() -> None:
        try:
            with ctx.write_session() as session:
                dest = transfers.check_move_destination(session, snap)
        except tr.StaleOperation as e:
            raise _DestinationLost(str(e)) from e
        seen = observe(store_for(ctx, snap.project, dest.storage_location), snap.path)
        if seen is None or (seen.hash, seen.size) != (snap.file_hash, snap.file_size):
            raise _DestinationLost(f"{dest.storage_location!r} no longer holds the moved content")

    try:
        store = store_for(ctx, snap.project, snap.storage_location)
        outcome = delete_object(
            store, snap, expected_hash=snap.hash, before_delete=destination_holds
        )
    except _DestinationLost as e:
        logger.warning(
            "repair of copy %s: not deleting the source of an interrupted move: %s",
            snap.copy_id,
            e,
        )
        try:
            with ctx.write_session() as session:
                transfers.abandon_source_delete(session, snap)
        except Exception:
            logger.exception("repair of copy %s: settling the source failed", snap.copy_id)
            return RepairResult.INCOMPLETE
        return RepairResult.REPAIRED
    except Exception:
        logger.exception("repair of copy %s: checking the move destination failed", snap.copy_id)
        return RepairResult.INCOMPLETE
    try:
        with ctx.write_session() as session:
            models.finish_delete(
                session, snap.file_id, [snap], {snap.copy_id: outcome}, updated_by=REPAIR_USER
            )
    except Exception:
        logger.exception("repair of copy %s: recording the delete failed", snap.copy_id)
        return RepairResult.INCOMPLETE
    if outcome == FileState.DELETING:
        return RepairResult.INCOMPLETE
    return RepairResult.REPAIRED

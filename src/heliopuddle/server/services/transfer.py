"""Verified transfers and source-only deletes for archiving rules (SVC-19/20).

See docs/design/operations.md. The result types and decisions are in
:mod:`.transfer_support`, the config and ownership guards in
:mod:`.transfer_guards`; the Prefect tasks and the store I/O are here.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from importlib import import_module
from typing import Literal

from prefect import task
from prefect.cache_policies import NO_CACHE

from heliopuddle.config.models import RuleOptions
from heliopuddle.server.context import AppContext, get_context
from heliopuddle.server.models import transfers, uploads
from heliopuddle.server.models import transitions as tr
from heliopuddle.server.models.snapshots import CopySnapshot
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services import delete, upload
from heliopuddle.server.services.concurrency import (
    ensure_concurrency_limit,
    hp_concurrency,
    storage_location_limit,
)
from heliopuddle.server.services.run_logging import run_logger
from heliopuddle.server.services.stores import Observation, observe, store_for
from heliopuddle.server.services.transfer_guards import (
    _adopt_identical,
    _check_config,
    _checkpoint,
    _configured,
    _expire,
    _owned_destination,
    _plan,
    _renewing,
    _space,
)
from heliopuddle.server.services.transfer_io import _copy_bytes, _refuse_same_object
from heliopuddle.server.services.transfer_support import (
    InsufficientSpace,
    Outcome,
    TransferResult,
    TransferVerificationError,
    _decision,
    _describe,
    _matches,
    _recorded,
    _skip,
    _validate_request,
    _verified,
)
from heliopuddle.server.services.upload.objects import (
    _move_into_place,
    _replace,
    _rm,
    _rm_quietly,
)
from heliopuddle.storage import StorageLocation
from heliopuddle.storage.location import backup_path, staging_path

logger = run_logger(__name__)


def _notify_file_changes(
    ctx: AppContext,
    project: str,
    file_ids: Iterable[int],
    *,
    storage_locations: Collection[str] | None = None,
) -> None:
    """Load the rule-control boundary only when a committed change needs dispatch."""
    notify = import_module("heliopuddle.server.services.rule_control").notify_file_changes
    notify(ctx, project, file_ids, storage_locations=storage_locations)


def _notify_change(
    ctx: AppContext,
    source: CopySnapshot,
    *,
    storage_locations: Collection[str] | None = None,
) -> None:
    try:
        _notify_file_changes(
            ctx, source.project, [source.file_id], storage_locations=storage_locations
        )
    except Exception:
        logger.exception(
            "rule notification failed after committed change to %s/%s", source.project, source.path
        )


def _restore_backup(
    ctx: AppContext,
    plan: transfers.TransferSnapshot,
    snap: CopySnapshot,
    store: StorageLocation,
    backup: str,
    original: Observation | None,
) -> None:
    """Best-effort restoration after a failed publish, fenced by destination ownership."""
    if original is None or not snap.staged:
        return
    try:
        saved = observe(store, backup)
        if saved is None or (saved.hash, saved.size) != (original.hash, original.size):
            return
        final = observe(store, snap.path)
        if _matches(final, plan.source) or (
            final is not None and (final.hash, final.size) == (original.hash, original.size)
        ):
            return
        _owned_destination(ctx, plan, snap)
        _replace(store, backup, snap.path)
    except Exception:
        logger.exception("restoring %s failed; its backup is left for repair", snap.path)


def _discard_partial(
    ctx: AppContext,
    plan: transfers.TransferSnapshot,
    snap: CopySnapshot,
    store: StorageLocation,
    write_path: str,
) -> None:
    """Remove only this failed write's partial bytes, never complete or taken-over content."""
    try:
        partial = observe(store, write_path)
        if partial is None or _matches(partial, plan.source):
            return
        _owned_destination(ctx, plan, snap)
        _rm(store, write_path)
    except (tr.StaleOperation, tr.CopyBusy):
        logger.info("transfer cleanup of %s skipped: its owner changed", write_path)
    except Exception:
        logger.exception("partial transfer %s is left for repair", write_path)


def _write_destination(
    ctx: AppContext,
    plan: transfers.TransferSnapshot,
    source_store: StorageLocation,
    destination_store: StorageLocation,
    options: RuleOptions,
    user: str | None,
) -> CopySnapshot | TransferResult:
    present = upload.object_present(ctx, plan.source.project, plan.destination, plan.source.path)
    with ctx.write_session() as session:
        snap = transfers.reserve_destination(
            session, plan, staged=present, lease=ctx.settings.op_lease, user=user
        )
    assert snap.token is not None
    staged = staging_path(snap.path, snap.token)
    backup = backup_path(snap.path, snap.token)
    current: Observation | None = None
    try:
        with _renewing(ctx, snap) as renewal_check:

            def check() -> None:
                renewal_check()
                _owned_destination(ctx, plan, snap)

            _checkpoint(ctx, plan, snap)
            current = observe(destination_store, snap.path)
            if current is not None and not snap.staged:
                with ctx.write_session() as session:
                    snap = transfers.stage_destination(session, plan, snap)
            check()
            decision = _decision(current, plan.source, options)
            if decision in {"newer", "conflict"}:
                assert current is not None
                _check_config(ctx, plan)
                with ctx.write_session() as session:
                    transfers.guard_destination(session, plan, snap)
                    uploads.finish_new(
                        session, snap, size=current.size, mtime=current.mtime, hash=current.hash
                    )
                _notify_change(ctx, plan.source, storage_locations={plan.destination})
                skipped = _skip(plan.source, decision)
                # A destination newly recorded as unknown is a change, like the conflict path.
                return TransferResult(skipped.file_id, skipped.path, "conflict", skipped.detail)
            if decision == "same":
                verified = _verified(current, plan.source)
            else:
                _checkpoint(ctx, plan, snap)
                write_path = staged if snap.staged else snap.path
                try:
                    _copy_bytes(source_store, destination_store, snap.path, write_path, check)
                except BaseException:
                    _discard_partial(ctx, plan, snap, destination_store, write_path)
                    raise
                verified = _verified(observe(destination_store, write_path), plan.source)
                check()
                if snap.staged:
                    _checkpoint(ctx, plan, snap)
                    _move_into_place(destination_store, snap.path, staged, backup)
                    verified = _verified(observe(destination_store, snap.path), plan.source)
            check()
            _check_config(ctx, plan)
            with ctx.write_session() as session:
                result = transfers.finish_transfer(
                    session,
                    plan,
                    snap,
                    size=verified.size,
                    mtime=verified.mtime,
                    hash=verified.hash,
                )
        _rm_quietly(destination_store, backup)
        return result
    except BaseException:
        _restore_backup(ctx, plan, snap, destination_store, backup, current)
        _expire(ctx, snap)
        raise


def _delete_source(
    ctx: AppContext,
    source: CopySnapshot,
    source_url: str,
    *,
    user: str | None,
    destination: CopySnapshot | None = None,
    destination_url: str | None = None,
) -> TransferResult:
    _configured(ctx, source.project, source.storage_location, source_url)
    source_store = store_for(ctx, source.project, source.storage_location)
    observed = observe(source_store, source.path)
    if observed is not None and not _matches(observed, source):
        raise tr.StaleOperation(f"source content of {source.path!r} changed before deletion")
    with ctx.write_session() as session:
        owned = transfers.begin_source_delete(
            session, source, source_url, ctx.settings.op_lease, destination=destination
        )

    def before_delete() -> None:
        _configured(ctx, source.project, source.storage_location, source_url)
        if destination is not None and destination_url is not None:
            _configured(ctx, source.project, destination.storage_location, destination_url)
        with ctx.write_session() as session:
            tr.lock_copy(
                session,
                owned.copy_id,
                token=owned.token,
                generation=owned.generation,
                expected_hash=source.file_hash,
                states={FileState.DELETING},
            )
            transfers.guard_storage_location(session, source.storage_location_id, source_url)
            if destination is not None and destination_url is not None:
                transfers.guard_source(session, destination, destination_url)
        if destination is not None:
            # The DB cannot see store-side changes made since the read-back.
            _verified(
                observe(store_for(ctx, source.project, destination.storage_location), source.path),
                source,
            )

    try:
        outcome = delete.delete_object(
            source_store, owned, expected_hash=source.file_hash, before_delete=before_delete
        )
    except BaseException:
        # A refused guard must not become an ownerless delete which repair would
        # retry after the move's destination stopped being safe.
        try:
            with ctx.write_session() as session:
                transfers.finish_source_delete(session, owned, FileState.UNKNOWN, user=user)
        except Exception:
            logger.exception("could not settle refused source delete %s", owned.copy_id)
        else:
            _notify_change(ctx, source)
        raise
    with ctx.write_session() as session:
        transfers.finish_source_delete(session, owned, outcome, user=user)
    _notify_change(ctx, source)
    if outcome != FileState.DELETED:
        raise RuntimeError(
            f"deleting {source.path!r} from {source.storage_location!r} left it {outcome.value}"
        )
    return TransferResult(source.file_id, source.path, "deleted")


@task(
    name="transfer_file",
    task_run_name="transfer_file-{file_id}-{source}-{destination}",
    cache_policy=NO_CACHE,
)
def transfer_file(
    file_id: int,
    source: str,
    destination: str,
    *,
    action: Literal["copy", "move"] = "copy",
    options: RuleOptions | None = None,
    user: str | None = None,
) -> TransferResult:
    """Copy or move one verified source revision under both storage-location limits.

    Args:
        file_id: the logical file.
        source: source storage location name.
        destination: destination storage location name in the same project.
        action: copy, or move after successful destination verification.
        options: rule transfer options; omitted fields use their configured defaults.
        user: user recorded on successful source deletion.

    Returns:
        A completed decision; store, verification, ownership and space failures raise.
    """
    result = _transfer_file(file_id, source, destination, action, options, user)
    logger.info(
        "transfer %s of file %s %s: %s -> %s",
        action,
        file_id,
        _describe(result),
        source,
        destination,
    )
    return result


def _transfer_file(
    file_id: int,
    source: str,
    destination: str,
    action: Literal["copy", "move"],
    options: RuleOptions | None,
    user: str | None,
) -> TransferResult:
    _validate_request(source, destination, action)
    ctx = get_context()
    options = options or RuleOptions()
    with ctx.sessionmaker() as session:
        initial, _url = transfers.source_snapshot(session, file_id, source)
    names = sorted(storage_location_limit(initial.project, name) for name in (source, destination))
    for name in names:
        ensure_concurrency_limit(name)
    with hp_concurrency(names, strict=True):
        plan = _plan(ctx, file_id, source, destination, dry_run=options.dry_run)
        _check_config(ctx, plan)
        source_store = store_for(ctx, initial.project, source)
        destination_store = store_for(ctx, initial.project, destination)
        _refuse_same_object(plan, source_store, destination_store)
        observed = observe(destination_store, plan.source.path)
        decision = _decision(observed, plan.source, options)
        if decision in {"newer", "conflict"}:
            return _skip_or_record(ctx, plan, observed, decision, options)
        if decision == "write":
            # Only a write needs room: an identical destination never fails on space.
            _space(ctx, plan, destination_store, options)
        if options.dry_run:
            return TransferResult(
                file_id, plan.source.path, "would_move" if action == "move" else "would_copy"
            )
        if decision == "same":
            adopted = _adopt_identical(ctx, plan, observed, action)
            if isinstance(adopted, TransferResult):
                return adopted
            ready, changed = adopted
        else:
            changed = True
            written = _write_destination(ctx, plan, source_store, destination_store, options, user)
            if isinstance(written, TransferResult):
                return written
            ready = written
        if changed:
            _notify_change(
                ctx, plan.source, storage_locations={destination} if action == "copy" else None
            )
        if action == "move":
            _finish_move(ctx, plan, ready, arrived=decision == "same", user=user)
        return TransferResult(file_id, plan.source.path, "moved" if action == "move" else "copied")


def _skip_or_record(
    ctx: AppContext,
    plan: transfers.TransferSnapshot,
    observed: Observation | None,
    decision: str,
    options: RuleOptions,
) -> TransferResult:
    """Skip a newer or conflicting destination; a conflict is newly recorded as ``unknown``."""
    if decision == "conflict" and not options.dry_run:
        assert observed is not None
        if _recorded(plan.destination_copy, observed, available=False):
            return _skip(plan.source, decision, recorded=False)
        _check_config(ctx, plan)
        with ctx.write_session() as session:
            transfers.record_destination(
                session,
                plan,
                size=observed.size,
                mtime=observed.mtime,
                hash=observed.hash,
                available=False,
            )
        _notify_change(ctx, plan.source, storage_locations={plan.destination})
    return _skip(plan.source, decision, recorded=not options.dry_run)


def _finish_move(
    ctx: AppContext,
    plan: transfers.TransferSnapshot,
    ready: CopySnapshot,
    *,
    arrived: bool,
    user: str | None,
) -> None:
    """Delete the source of a move once the destination is verified."""
    if arrived:
        # Already identical there, recorded or not: the move still brings
        # the file into it, so its arrival restarts (SVC-29).
        with ctx.write_session() as session:
            ready = transfers.mark_arrived(session, plan, ready)
    _delete_source(
        ctx,
        plan.source,
        plan.source_url,
        user=user,
        destination=ready,
        destination_url=plan.destination_url,
    )


@task(
    name="delete_source_copy",
    task_run_name="delete_source_copy-{file_id}-{source}",
    cache_policy=NO_CACHE,
)
def delete_source_copy(
    file_id: int,
    source: str,
    *,
    expected_generation: int | None = None,
    expected_hash: str | None = None,
    user: str | None = None,
) -> TransferResult:
    """Delete only the named source copy, through the guarded delete protocol.

    Args:
        file_id: the logical file.
        source: source storage location name.
        expected_generation: optional revision captured before a move.
        expected_hash: optional accepted hash captured before a move.
        user: user recorded on successful deletion.

    Returns:
        ``deleted`` only after its guarded finish; incomplete or stale deletes raise.
    """
    ctx = get_context()
    with ctx.sessionmaker() as session:
        snap, _url = transfers.source_snapshot(session, file_id, source)
    limit = storage_location_limit(snap.project, source)
    ensure_concurrency_limit(limit)
    with hp_concurrency(limit, strict=True):
        with ctx.sessionmaker() as session:
            snap, url = transfers.source_snapshot(
                session,
                file_id,
                source,
                expected_generation=expected_generation,
                expected_hash=expected_hash,
            )
        result = _delete_source(ctx, snap, url, user=user)
    logger.info(
        "deleted source copy of file %s %r from storage location %s", file_id, result.path, source
    )
    return result


__all__ = [
    "InsufficientSpace",
    "Observation",
    "Outcome",
    "TransferResult",
    "TransferVerificationError",
    "delete_source_copy",
    "transfer_file",
]

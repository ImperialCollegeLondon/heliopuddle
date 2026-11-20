"""Config, ownership and lease guards around a verified transfer (SVC-19/20).

See docs/design/operations.md. These functions fence every store write by the
DB state: the configured storage locations must be unchanged and the
destination copy must still be owned by the transfer's token.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable, Iterator
from typing import Literal

from heliopuddle.config.models import RuleOptions
from heliopuddle.server.context import AppContext
from heliopuddle.server.models import transfers, uploads
from heliopuddle.server.models import transitions as tr
from heliopuddle.server.models.snapshots import CopySnapshot
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services import upload
from heliopuddle.server.services.run_logging import run_logger
from heliopuddle.server.services.stores import Observation
from heliopuddle.server.services.transfer_support import (
    InsufficientSpace,
    TransferResult,
    _recorded,
    _verified,
)
from heliopuddle.storage import StorageLocation

logger = run_logger("heliopuddle.server.services.transfer")


def _configured(ctx: AppContext, project: str, name: str, url: str) -> None:
    cfg = ctx.storage_location_config(project, name)
    if cfg is None or cfg.url != url:
        raise tr.StaleOperation(f"configured storage location {project}/{name} changed")


def _check_config(ctx: AppContext, plan: transfers.TransferSnapshot) -> None:
    _configured(ctx, plan.source.project, plan.source.storage_location, plan.source_url)
    _configured(ctx, plan.source.project, plan.destination, plan.destination_url)


def _plan(
    ctx: AppContext, file_id: int, source: str, destination: str, *, dry_run: bool
) -> transfers.TransferSnapshot:
    repaired: set[int] = set()
    while True:
        try:
            with ctx.sessionmaker() as session:
                return transfers.transfer_snapshot(session, file_id, source, destination)
        except tr.NeedsRepair as error:
            if dry_run or error.copy_id in repaired:
                raise
            repaired.add(error.copy_id)
            if upload.repair_expired(error.copy_id) == upload.RepairResult.INCOMPLETE:
                raise RuntimeError(f"repairing destination copy {error.copy_id} failed") from error


def _space(
    ctx: AppContext,
    plan: transfers.TransferSnapshot,
    destination: StorageLocation,
    options: RuleOptions,
) -> None:
    if not options.check_for_space:
        return
    cfg = ctx.storage_location_config(plan.source.project, plan.destination)
    if cfg is None:
        raise LookupError(f"storage location {plan.destination!r} is not configured")
    with ctx.sessionmaker() as session:
        used = transfers.indexed_size(session, plan.destination_id)
    free = destination.free_space(quota=cfg.quota, indexed_size=used)
    required = plan.source.file_size + int(options.check_for_space_overhead)
    if free is not None and free < required:
        raise InsufficientSpace(
            f"storage location {plan.destination!r} has {free} bytes free; "
            f"{required} bytes required (source plus overhead)"
        )


def _checkpoint(
    ctx: AppContext, plan: transfers.TransferSnapshot, destination: CopySnapshot
) -> None:
    _check_config(ctx, plan)
    with ctx.write_session() as session:
        transfers.guard_source(session, plan.source, plan.source_url)
        transfers.guard_destination(session, plan, destination)


def _owned_destination(
    ctx: AppContext, plan: transfers.TransferSnapshot, snap: CopySnapshot
) -> None:
    """Fence store writes and cleanup even if the source's logical content changed."""
    _configured(ctx, snap.project, snap.storage_location, plan.destination_url)
    with ctx.write_session() as session:
        tr.lock_copy(
            session,
            snap.copy_id,
            token=snap.token,
            generation=snap.generation,
            states={FileState.PENDING},
        )
        transfers.guard_storage_location(session, plan.destination_id, plan.destination_url)


def _expire(ctx: AppContext, snap: CopySnapshot) -> None:
    try:
        with ctx.write_session() as session:
            uploads.abandon(session, snap)
    except Exception:
        logger.exception("transfer copy %s will be repaired after its lease expires", snap.copy_id)


@contextlib.contextmanager
def _renewing(ctx: AppContext, snap: CopySnapshot) -> Iterator[Callable[[], None]]:
    """Renew the destination while blocking store calls run; stop on every exit path."""
    stopped = threading.Event()
    errors: list[Exception] = []
    assert snap.token is not None
    token = snap.token

    def renew() -> None:
        interval = max(0.001, ctx.settings.op_lease.total_seconds() / 3)
        while not stopped.wait(interval):
            try:
                with ctx.write_session() as session:
                    uploads.renew(session, token, ctx.settings.op_lease)
            except Exception as error:
                errors.append(error)
                return

    def check() -> None:
        if errors:
            raise errors[0]

    worker = threading.Thread(target=renew, name=f"transfer-lease-{snap.copy_id}", daemon=True)
    worker.start()
    try:
        yield check
    finally:
        stopped.set()
        worker.join()


def _adopt_identical(
    ctx: AppContext,
    plan: transfers.TransferSnapshot,
    observed: Observation | None,
    action: Literal["copy", "move"],
) -> TransferResult | tuple[CopySnapshot, bool]:
    """Record an identical destination.

    Returns:
        ``unchanged`` when a copy finds it already recorded; otherwise the
        destination copy and whether recording it changed anything.
    """
    verified = _verified(observed, plan.source)
    _check_config(ctx, plan)
    if _recorded(plan.destination_copy, verified, available=True):
        if action == "copy":
            return TransferResult(plan.source.file_id, plan.source.path, "unchanged")
        assert plan.destination_copy is not None
        return plan.destination_copy, False
    with ctx.write_session() as session:
        ready = transfers.record_destination(
            session,
            plan,
            size=verified.size,
            mtime=verified.mtime,
            hash=verified.hash,
            available=True,
        )
    return ready, True

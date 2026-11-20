"""The DB half of deleting a file: own its copies, then record the outcome."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping, Sequence
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from heliopuddle.server.database.orm import File, utcnow
from heliopuddle.server.models import transitions
from heliopuddle.server.models.files.errors import NotFoundError
from heliopuddle.server.models.files.query import _file_options
from heliopuddle.server.models.series import lock_series_of_file, recompute_latest
from heliopuddle.server.models.snapshots import CopySnapshot, copy_snapshot
from heliopuddle.server.models.transitions import (
    CopyBusy,
    NeedsRepair,
    StaleOperation,
    bump,
    is_owned,
    lock_copy,
    release,
    take,
)
from heliopuddle.server.schemas import FileState

logger = logging.getLogger(__name__)


DEFAULT_LEASE = timedelta(hours=1)
"""Ownership lease used when a caller does not pass one (``HELIOPUDDLE_OP_LEASE``)."""


def begin_delete(
    session: Session,
    file_id: int,
    *,
    updated_by: str | None = None,
    lease: timedelta = DEFAULT_LEASE,
) -> tuple[str, list[CopySnapshot]]:
    """Own every copy of a live file for a delete (step 1 of "Delete").

    Under the series lock, every copy that is not ``deleted`` becomes
    ``deleting`` with its own new token (generation bumped), which blocks other
    operations on those paths. A ``deleting`` copy without a token, or with an
    expired one, is a delete that did not finish and is taken over. Sets
    ``updated_by`` and recomputes ``is_latest`` (a file whose copies are all
    ``deleting`` is not eligible).

    Args:
        session: a session in a write transaction; the caller commits.
        file_id: the file.
        updated_by: user id.
        lease: how long the delete owns the copies.

    Returns:
        The project name and a snapshot of each owned copy (each with its
        delete token).

    Raises:
        NotFoundError: if the file does not exist or is already deleted.
        CopyBusy: another live operation owns a copy.
        NeedsRepair: a copy holds an expired upload or overwrite.
    """
    series = lock_series_of_file(session, file_id)
    if series is None:
        raise NotFoundError(f"file {file_id} not found")
    file = session.scalars(
        select(File)
        .where(File.id == file_id)
        .with_for_update(of=File)
        .options(*_file_options())
        .execution_options(populate_existing=True)
    ).one()
    if file.deleted_at is not None:
        raise NotFoundError(f"file {file_id} not found")
    now = transitions.clock()
    live = [c for c in file.copies if c.state != FileState.DELETED]
    for copy in live:
        if is_owned(copy, now):
            raise CopyBusy(copy.id, copy.op_deadline)
        if copy.op_token is not None and copy.state != FileState.DELETING:
            raise NeedsRepair(copy.id)
    for copy in live:
        take(copy, FileState.DELETING, str(uuid.uuid4()), now + lease)
        bump(copy)
    file.updated_by = updated_by
    file.updated_at = utcnow()
    session.flush()
    recompute_latest(session, series)
    return file.project.name, [copy_snapshot(session, c) for c in live]


_DELETE_OUTCOMES = frozenset({FileState.DELETED, FileState.UNKNOWN, FileState.DELETING})


def finish_delete(
    session: Session,
    file_id: int,
    copies: Sequence[CopySnapshot],
    outcomes: Mapping[int, FileState] | None = None,
    *,
    updated_by: str | None = None,
) -> File:
    """Record what the store step of a delete did; soft-delete the file when no copy is left.

    Each of ``copies`` (from :func:`begin_delete`) that is still ``deleting``
    with its delete token is released, as ``outcomes`` says: ``deleted``
    (removed from the store, generation bumped),
    ``unknown`` (the object did not match hp's last observation and was left
    in the store) or ``deleting`` without a token (the store delete failed;
    retried later). Copies the delete no longer owns are left alone. If every
    copy is ``deleted``, the file gets ``deleted_at`` and ``updated_by``.
    ``is_latest`` is recomputed. A file that is already deleted is returned
    unchanged.

    Args:
        session: a session in a write transaction; the caller commits.
        file_id: the file.
        copies: the owned copies, with their tokens.
        outcomes: the state for each copy id; None (or a missing id) means
            ``deleted``.
        updated_by: user id.

    Returns:
        The file.

    Raises:
        NotFoundError: if the file does not exist.
        ValueError: if an outcome is not ``deleted``, ``unknown`` or ``deleting``.
    """
    results = {c.copy_id: (outcomes or {}).get(c.copy_id, FileState.DELETED) for c in copies}
    tokens = {c.copy_id: c.token for c in copies}
    if not set(results.values()) <= _DELETE_OUTCOMES:
        raise ValueError(f"invalid delete outcomes: {results}")
    series = lock_series_of_file(session, file_id)
    if series is None:
        raise NotFoundError(f"file {file_id} not found")
    file = session.scalars(
        select(File)
        .where(File.id == file_id)
        .with_for_update(of=File)
        .options(*_file_options())
        .execution_options(populate_existing=True)
    ).one()
    if file.deleted_at is not None:
        return file
    for copy_id, state in results.items():
        try:
            copy = lock_copy(
                session,
                copy_id,
                token=tokens[copy_id],
                states={FileState.DELETING},
                require_live=False,
            )
        except (StaleOperation, CopyBusy) as e:
            logger.info("finish_delete(%s): copy %s skipped: %s", file_id, copy_id, e)
            continue
        if copy.file_id != file_id:
            continue
        release(copy, state)
        if state == FileState.DELETED:
            bump(copy)
    session.refresh(file, ["copies"])
    if all(c.state == FileState.DELETED for c in file.copies):
        now = utcnow()
        file.deleted_at = now
        file.updated_by = updated_by
        file.updated_at = now
    recompute_latest(session, series)
    return file

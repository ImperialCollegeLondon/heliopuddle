"""Settle a ``pending`` new-file upload from what the store holds."""

from __future__ import annotations

from heliopuddle.server.context import AppContext
from heliopuddle.server.models import transitions as tr
from heliopuddle.server.models import uploads
from heliopuddle.server.models.snapshots import CopySnapshot
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services.run_logging import run_logger
from heliopuddle.server.services.stores import Observation, observe
from heliopuddle.server.services.upload.objects import (
    _matches,
    _move_into_place,
    _mv,
    _present,
    _replace,
    _rm,
    _rm_quietly,
)
from heliopuddle.server.services.upload.results import _Settled
from heliopuddle.storage import StorageLocation
from heliopuddle.storage.location import backup_path, parse_temp_path

logger = run_logger("heliopuddle.server.services.upload")


def _settle_new(
    ctx: AppContext,
    store: StorageLocation,
    snap: CopySnapshot,
    staged: list[str] | None = None,
    backups: list[str] | None = None,
    published_token: str | None = None,
) -> _Settled:
    """Settle a ``pending`` new-file upload from the final object.

    A final object matching the reserved hash and size is verified as normal
    (``available``). Otherwise a reservation that created the copy is dropped
    with its object; one on an existing copy leaves the object and marks the
    copy ``unknown`` (or ``missing`` without an object). A staged reservation
    (an object existed at the final path when it was reserved) is settled by
    :func:`_settle_staged_new` and never removes the object at the final path.

    Args:
        ctx: the app context.
        store: the copy's storage location.
        snap: the claimed upload.
        staged: the reservation's staging files (staged reservations only).
        backups: its backup files (staged reservations only).
        published_token: the client token to record if a repair publishes it.
    """
    if snap.staged:
        return _settle_staged_new(ctx, store, snap, staged or [], backups or [], published_token)
    try:
        obs = observe(store, snap.path)
        if _matches(obs, snap.pending_hash, snap.pending_size) and obs is not None:
            with ctx.write_session() as session:
                state = uploads.finish_new(
                    session,
                    snap,
                    size=obs.size,
                    mtime=obs.mtime,
                    hash=obs.hash,
                    published_token=published_token,
                )
            return _Settled.PUBLISHED if state == FileState.AVAILABLE else _Settled.UNKNOWN
        if snap.created_by_reservation:
            if obs is not None:
                _rm(store, snap.path)
            with ctx.write_session() as session:
                uploads.drop_new(session, snap, object_present=False)
            return _Settled.DROPPED
        with ctx.write_session() as session:
            uploads.drop_new(session, snap, object_present=obs is not None)
        return _Settled.UNKNOWN
    except (tr.StaleOperation, tr.CopyBusy) as e:
        logger.info("upload of %s: settle dropped: %s", snap.path, e)
        return _Settled.STALE
    except Exception:
        logger.exception("upload of %s: settling failed; it will be repaired", snap.path)
        return _Settled.INCOMPLETE


def _settle_staged_new(
    ctx: AppContext,
    store: StorageLocation,
    snap: CopySnapshot,
    staged: list[str],
    backups: list[str],
    published_token: str | None = None,
) -> _Settled:
    """Settle a ``pending`` upload that wrote a staging path over an existing object.

    1. The final object is the reserved content: record the publish.
    2. The final object is missing or does not verify and a backup exists
       (a publish was cut short): restore the backup.
    3. A staged file holds the reserved content: publish it (the existing
       object is backed up first and restored if the move fails).
    4. Otherwise the staged file is removed. The object at the final path is
       never removed: a reservation that created the copy is dropped, one on
       an existing copy becomes ``unknown`` (``missing`` without an object).
    """
    final = snap.path
    try:
        obs = observe(store, final)
        if _matches(obs, snap.pending_hash, snap.pending_size) and obs is not None:
            return _finish_staged(ctx, store, snap, obs, staged, backups, published_token)
        restored = False
        for backup in backups:
            if observe(store, backup) is not None:
                if obs is None:
                    _mv(store, backup, final)
                else:
                    _replace(store, backup, final)
                restored = True
                break
        # A transfer uses pending upload ownership without changing the logical
        # hash. Once another copy changes that hash, its staged bytes are stale.
        current_content = (snap.pending_hash, snap.pending_size) == (
            snap.file_hash,
            snap.file_size,
        )
        if not restored and current_content:
            for candidate in staged:
                if _matches(observe(store, candidate), snap.pending_hash, snap.pending_size):
                    published = _publish_candidate(store, final, candidate, snap)
                    if published is not None:
                        return _finish_staged(
                            ctx, store, snap, published, staged, backups, published_token
                        )
        _rm_quietly(store, *staged)
        present = _present(store, final)
        with ctx.write_session() as session:
            uploads.drop_new(session, snap, object_present=present)
        _rm_quietly(store, *backups)
        return _Settled.DROPPED if snap.created_by_reservation else _Settled.UNKNOWN
    except (tr.StaleOperation, tr.CopyBusy) as e:
        logger.info("upload of %s: settle dropped: %s", snap.path, e)
        return _Settled.STALE
    except Exception:
        logger.exception("upload of %s: settling failed; it will be repaired", snap.path)
        return _Settled.INCOMPLETE


def _publish_candidate(
    store: StorageLocation, final: str, candidate: str, snap: CopySnapshot
) -> Observation | None:
    """Publish a staged file at ``final``; return the verified final object, or None."""
    parsed = parse_temp_path(candidate)
    backup = backup_path(final, parsed.token) if parsed else candidate + ".hp-old"
    try:
        _move_into_place(store, final, candidate, backup)
    except Exception:
        logger.warning("publishing %s from %s failed", final, candidate)
        return None
    obs = observe(store, final)
    if _matches(obs, snap.pending_hash, snap.pending_size):
        return obs
    return None


def _finish_staged(
    ctx: AppContext,
    store: StorageLocation,
    snap: CopySnapshot,
    obs: Observation,
    staged: list[str],
    backups: list[str],
    published_token: str | None,
) -> _Settled:
    with ctx.write_session() as session:
        state = uploads.finish_new(
            session,
            snap,
            size=obs.size,
            mtime=obs.mtime,
            hash=obs.hash,
            published_token=published_token,
        )
    leftovers = [
        backup_path(snap.path, parsed.token) for c in staged if (parsed := parse_temp_path(c))
    ]
    _rm_quietly(store, *staged, *backups, *leftovers)
    return _Settled.PUBLISHED if state == FileState.AVAILABLE else _Settled.UNKNOWN

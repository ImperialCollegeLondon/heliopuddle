"""Settle an ``overwriting`` copy from what the store holds."""

from __future__ import annotations

from heliopuddle.server.context import AppContext
from heliopuddle.server.models import transitions as tr
from heliopuddle.server.models import uploads
from heliopuddle.server.models.snapshots import CopySnapshot
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services.run_logging import run_logger
from heliopuddle.server.services.stores import Observation, observe
from heliopuddle.server.services.upload.objects import _matches, _replace, _rm_quietly
from heliopuddle.server.services.upload.results import _Settled
from heliopuddle.storage import StorageLocation

logger = run_logger("heliopuddle.server.services.upload")


def _settle_overwrite(
    ctx: AppContext,
    store: StorageLocation,
    snap: CopySnapshot,
    staged: list[str],
    backups: list[str],
    user: str | None = None,
    published_token: str | None = None,
) -> _Settled:
    """Settle an ``overwriting`` copy from what the store holds.

    1. The final object is the new content: record the publish.
    2. It is the original: the copy returns to ``available``.
    3. Otherwise (missing or wrong): restore a backup holding the original,
       else publish a staged file holding the new content, else the copy
       becomes ``unknown``.

    Staging and backup files are removed once the DB records the outcome.
    """
    final = snap.path
    try:
        obs = observe(store, final)
        if _matches(obs, snap.pending_hash, snap.pending_size) and obs is not None:
            _finish_overwrite(ctx, snap, obs, user, published_token)
            _rm_quietly(store, *staged, *backups)
            return _Settled.PUBLISHED
        if obs is not None and obs.hash == snap.file_hash:
            _release(ctx, snap, obs)
            _rm_quietly(store, *staged, *backups)
            return _Settled.ORIGINAL
        for backup in backups:
            if (b := observe(store, backup)) is not None and b.hash == snap.file_hash:
                _replace(store, backup, final)
                restored = observe(store, final)
                if restored is not None and restored.hash == snap.file_hash:
                    _release(ctx, snap, restored)
                    _rm_quietly(store, *staged, *backups)
                    return _Settled.ORIGINAL
        # Staged content is published only while the file is still the content the
        # overwrite was based on (the copy's observed hash): another location may
        # have replaced the file since, and a stale overwrite must not undo that.
        based_on_current = snap.hash is None or snap.hash == snap.file_hash
        for candidate in staged if based_on_current else []:
            if _matches(observe(store, candidate), snap.pending_hash, snap.pending_size):
                _replace(store, candidate, final)
                published = observe(store, final)
                if _matches(published, snap.pending_hash, snap.pending_size) and published:
                    _finish_overwrite(ctx, snap, published, user, published_token)
                    _rm_quietly(store, *staged, *backups)
                    return _Settled.PUBLISHED
        with ctx.write_session() as session:
            uploads.release_overwrite(
                session,
                snap,
                size=obs.size if obs else None,
                mtime=obs.mtime if obs else None,
            )
        _rm_quietly(store, *staged)
        return _Settled.UNKNOWN
    except (tr.StaleOperation, tr.CopyBusy) as e:
        logger.info("overwrite of %s: settle dropped: %s", final, e)
        return _Settled.STALE
    except Exception:
        logger.exception("overwrite of %s: settling failed; it will be repaired", final)
        return _Settled.INCOMPLETE


def _finish_overwrite(
    ctx: AppContext,
    snap: CopySnapshot,
    obs: Observation,
    user: str | None,
    published_token: str | None = None,
) -> None:
    """Record a published overwrite, or (a staged new upload) a published new file."""
    with ctx.write_session() as session:
        if snap.state == FileState.OVERWRITING:
            uploads.finish_overwrite(
                session,
                snap,
                size=obs.size,
                mtime=obs.mtime,
                hash=obs.hash,
                user=user,
                published_token=published_token,
            )
        else:
            uploads.finish_new(
                session,
                snap,
                size=obs.size,
                mtime=obs.mtime,
                hash=obs.hash,
                published_token=published_token,
            )


def _release(ctx: AppContext, snap: CopySnapshot, obs: Observation) -> None:
    with ctx.write_session() as session:
        uploads.release_overwrite(session, snap, size=obs.size, mtime=obs.mtime, hash=obs.hash)

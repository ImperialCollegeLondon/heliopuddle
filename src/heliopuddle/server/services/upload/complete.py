"""Complete, publish and abort an upload (the store half; STO-09, STO-10)."""

from __future__ import annotations

from heliopuddle.server.context import AppContext, get_context
from heliopuddle.server.models import transitions as tr
from heliopuddle.server.models import uploads
from heliopuddle.server.models.snapshots import CopySnapshot
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services.run_logging import run_logger
from heliopuddle.server.services.stores import observe, store_for
from heliopuddle.server.services.upload.objects import _matches, _move_into_place, _rm_quietly
from heliopuddle.server.services.upload.results import (
    _AFTER,
    _TO_REPAIR,
    CompleteResult,
    ObjectUnobservable,
    PublishFailed,
    RepairResult,
    _Settled,
)
from heliopuddle.server.services.upload.settle_new import _settle_new, _settle_staged_new
from heliopuddle.server.services.upload.settle_overwrite import _finish_overwrite, _settle_overwrite
from heliopuddle.storage import StorageLocation
from heliopuddle.storage.location import backup_path, staging_path

logger = run_logger("heliopuddle.server.services.upload")


def _abandon(ctx: AppContext, snap: CopySnapshot) -> None:
    """Let the next repair retry an operation that could not finish (best effort)."""
    try:
        with ctx.write_session() as session:
            uploads.abandon(session, snap)
    except Exception:
        logger.exception("could not release copy %s; it expires at its deadline", snap.copy_id)


def _store(ctx: AppContext, snap: CopySnapshot) -> StorageLocation:
    try:
        return store_for(ctx, snap.project, snap.storage_location)
    except LookupError:
        _abandon(ctx, snap)
        raise


def complete_upload(token: str, user: str | None = None) -> CompleteResult:
    """Complete an upload (``POST /uploads/{token}/complete``).

    The upload is claimed first (it gets a new token, so the client's token
    stops being current). A new-file upload is observed at its final path and
    recorded ``available`` (hash and size match) or ``unknown``. An overwrite
    is published (:func:`publish_overwrite`).

    Args:
        token: the client's upload token.
        user: user id (``updated_by`` on overwrite).

    Returns:
        The file and its copy's state.

    Raises:
        TokenNotCurrent: the token is unknown, finished or was taken over.
        PublishFailed: the overwrite did not publish (the original is kept).
        LookupError: the storage location is not in the loaded config.
    """
    ctx = get_context()
    try:
        with ctx.write_session() as session:
            snap = uploads.claim(session, token, ctx.settings.op_lease)
    except uploads.TokenNotCurrent:
        with ctx.sessionmaker() as session:
            file_id = uploads.published_by_repair(session, token)
        if file_id is None:
            raise
        # A repair adopted exactly what this client wrote: the upload succeeded.
        return CompleteResult(file_id, FileState.AVAILABLE)
    store = _store(ctx, snap)
    if snap.state == FileState.OVERWRITING or snap.staged:
        return publish_overwrite(ctx, store, snap, token, user)
    settled = _complete_new(ctx, store, snap)
    if settled == _Settled.UNOBSERVED:
        try:
            with ctx.write_session() as session:
                uploads.unclaim(session, snap, token, ctx.settings.op_lease)
        except Exception:
            logger.info("completing %s: could not hand the upload back", snap.path)
            _abandon(ctx, snap)
        raise ObjectUnobservable(snap.file_id, snap.path)
    if settled == _Settled.INCOMPLETE:
        _abandon(ctx, snap)
        raise RuntimeError(f"completing upload of {snap.path} failed; it will be repaired")
    state = _copy_state(ctx, snap)
    logger.info(
        "upload completed: %s/%s in storage location %s (file %s): %s",
        snap.project,
        snap.path,
        snap.storage_location,
        snap.file_id,
        state.value,
    )
    return CompleteResult(snap.file_id, state)


def _complete_new(ctx: AppContext, store: StorageLocation, snap: CopySnapshot) -> _Settled:
    try:
        try:
            obs = observe(store, snap.path)
        except Exception:
            logger.warning("completing %s: the object could not be observed", snap.path)
            return _Settled.UNOBSERVED
        with ctx.write_session() as session:
            uploads.finish_new(
                session,
                snap,
                size=obs.size if obs else None,
                mtime=obs.mtime if obs else None,
                hash=obs.hash if obs else None,
            )
        return _Settled.PUBLISHED
    except (tr.StaleOperation, tr.CopyBusy) as e:  # pragma: no cover - claimed just before
        logger.info("completing %s dropped: %s", snap.path, e)
        return _Settled.STALE
    except Exception:
        logger.exception("completing %s failed", snap.path)
        return _Settled.INCOMPLETE


def _copy_state(ctx: AppContext, snap: CopySnapshot) -> FileState:
    with ctx.sessionmaker() as session:
        current = uploads.snapshot(session, snap.copy_id)
        return current.state if current is not None else FileState.DELETED


def _settle_publish(
    ctx: AppContext,
    store: StorageLocation,
    snap: CopySnapshot,
    staged: list[str],
    backups: list[str],
    user: str | None = None,
) -> _Settled:
    """Settle a staged upload (an overwrite, or a new file staged over an existing object)."""
    if snap.state == FileState.OVERWRITING:
        return _settle_overwrite(ctx, store, snap, staged, backups, user)
    return _settle_staged_new(ctx, store, snap, staged, backups)


def publish_overwrite(
    ctx: AppContext,
    store: StorageLocation,
    snap: CopySnapshot,
    staging_token: str,
    user: str | None = None,
) -> CompleteResult:
    """Publish a staged overwrite (O2), or a new file staged over an existing object.

    A ``pending`` reservation that staged its write (an object already existed
    at the final path) is published the same way; its existing object is the
    "original" that is backed up and restored, and the DB step is the
    new-file completion (``available`` when hash and size match).

    1. Verify the staged object against ``pending_hash`` and ``pending_size``.
    2. Move it to the final path: directly where the backend's move replaces
       (``backends.move_replaces``); otherwise back up the original to
       ``<path>.<token>.hp-old.hp-tmp`` first, restoring it if the move fails.
    3. Re-verify the final object.
    4. Guarded DB update: new hash and size, ``available``, generation + 1,
       token cleared, other copies ``unknown``, latest recomputed.
    5. Delete the backup.

    Any failure settles the copy from what the store then holds (the
    original kept, or the new content published); a failure while settling
    leaves the copy owned with an expired deadline for repair.

    Args:
        ctx: the app context.
        store: the copy's storage location.
        snap: the claimed overwrite.
        staging_token: the token the staging and backup paths are named after.
        user: ``updated_by``.

    Returns:
        The file and ``available`` when published.

    Raises:
        PublishFailed: the new content was not published.
    """
    staged = staging_path(snap.path, staging_token)
    backup = backup_path(snap.path, staging_token)
    try:
        staged_obs = observe(store, staged)
        if not _matches(staged_obs, snap.pending_hash, snap.pending_size):
            _rm_quietly(store, staged)
            outcome = _settle_publish(ctx, store, snap, [], [backup], user)
            if outcome == _Settled.INCOMPLETE:
                _abandon(ctx, snap)
            raise PublishFailed(
                snap.file_id,
                f"the staged content of {snap.path!r} does not match the reserved hash "
                f"and size; {_AFTER.get(outcome, 'the copy will be repaired')}",
            )
        _move_into_place(store, snap.path, staged, backup)
        final_obs = observe(store, snap.path)
        if not _matches(final_obs, snap.pending_hash, snap.pending_size) or final_obs is None:
            raise RuntimeError(f"the published {snap.path!r} does not match the reserved hash")
        _finish_overwrite(ctx, snap, final_obs, user)
    except PublishFailed:
        raise
    except Exception as e:
        logger.warning(
            "publishing %s failed (%s); settling from the store", snap.path, type(e).__name__
        )
        outcome = _settle_publish(ctx, store, snap, [staged], [backup], user)
        if outcome == _Settled.PUBLISHED:
            return CompleteResult(snap.file_id, FileState.AVAILABLE)
        if outcome == _Settled.INCOMPLETE:
            _abandon(ctx, snap)
        raise PublishFailed(
            snap.file_id,
            f"publishing {snap.path!r} failed: {type(e).__name__}; {_AFTER[outcome]}",
        ) from e
    _rm_quietly(store, backup)
    logger.info(
        "upload published: %s/%s in storage location %s (file %s, %s bytes)",
        snap.project,
        snap.path,
        snap.storage_location,
        snap.file_id,
        snap.pending_size,
    )
    return CompleteResult(snap.file_id, FileState.AVAILABLE)


def abort_upload(token: str) -> RepairResult:
    """Abort an upload (``DELETE /uploads/{token}``).

    A new-file upload's object is removed unless it matches the reserved
    content (then it is recorded as if completed), and a reservation that
    created the copy is dropped. An overwrite's staged file is removed and the
    copy returns to ``available`` with the original.

    Returns:
        ``repaired``, ``dropped`` or ``incomplete`` (retried by repair).

    Raises:
        TokenNotCurrent: the token is unknown, finished or was taken over.
    """
    ctx = get_context()
    with ctx.write_session() as session:
        snap = uploads.claim(session, token, ctx.settings.op_lease)
    store = _store(ctx, snap)
    if snap.state == FileState.OVERWRITING:
        _rm_quietly(store, staging_path(snap.path, token))
        outcome = _settle_overwrite(
            ctx, store, snap, [staging_path(snap.path, token)], [backup_path(snap.path, token)]
        )
    elif snap.staged:
        _rm_quietly(store, staging_path(snap.path, token))
        outcome = _settle_staged_new(
            ctx, store, snap, [staging_path(snap.path, token)], [backup_path(snap.path, token)]
        )
    else:
        outcome = _settle_new(ctx, store, snap)
    if outcome == _Settled.INCOMPLETE:
        _abandon(ctx, snap)
    return _TO_REPAIR[outcome]

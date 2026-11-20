"""Remove files from their stores (SVC-07, STO-13). See docs/design/operations.md ("Delete").

``delete_file`` is the one place that removes a file. The API calls it inline
for ``DELETE /files/{id}``; the ``delete_files`` flow runs it over many files
for bulk use (served as the deployment ``delete_files/delete-files``); the
index retries unfinished deletes through ``repair_expired``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from prefect import flow, task

from heliopuddle.server import models
from heliopuddle.server.context import AppContext, get_context
from heliopuddle.server.models import transitions as tr
from heliopuddle.server.models.snapshots import CopySnapshot
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services.run_logging import run_logger
from heliopuddle.server.services.stores import ObservationChanged, observe, store_for
from heliopuddle.storage import StorageLocation

logger = run_logger(__name__)


def _unchanged(
    store: StorageLocation, snap: CopySnapshot, *, expected_hash: str | None = None
) -> FileState | None:
    """Compare the object with the copy's last observation.

    Returns:
        None when the object is what hp observed (safe to remove); otherwise
        the outcome to report: ``deleted`` (already gone), ``unknown`` (never
        observed, or changed) or ``deleting`` (the store could not be read).
    """
    try:
        info = store.info(snap.path)
    except FileNotFoundError:
        return FileState.DELETED
    except Exception:
        logger.exception("delete: reading %s in %s failed", snap.path, snap.storage_location)
        return FileState.DELETING
    if not snap.observed:
        logger.warning(
            "delete: %s in %s was never observed by hp; it is left in the store",
            snap.path,
            snap.storage_location,
        )
        return FileState.UNKNOWN
    try:
        changed = _changed_since_observed(store, snap, info)
    except FileNotFoundError:
        return FileState.DELETED
    if changed:
        logger.warning(
            "delete: %s in %s changed since hp last observed it; it is left in the store",
            snap.path,
            snap.storage_location,
        )
        return FileState.UNKNOWN
    return _recheck_hash(store, snap, expected_hash)


def _changed_since_observed(store: StorageLocation, snap: CopySnapshot, info: Any) -> bool:
    """Whether size, mtime or the native checksum differ from the observation.

    Raises:
        FileNotFoundError: the object vanished while its checksum was read.
    """
    if info.size != snap.size or (snap.mtime is not None and info.mtime != snap.mtime):
        return True
    if snap.hash is None:
        return False
    try:
        native = store.native_md5(snap.path)
    except FileNotFoundError:
        raise
    except Exception:
        native = None
    return native is not None and native != snap.hash


def _recheck_hash(
    store: StorageLocation, snap: CopySnapshot, expected_hash: str | None
) -> FileState | None:
    """Re-hash the object against ``expected_hash``; None when it matches or is unchecked."""
    if expected_hash is None:
        return None
    try:
        obs = observe(store, snap.path)
    except ObservationChanged:
        return FileState.UNKNOWN
    except Exception:
        logger.exception("delete: could not re-hash %s before removal", snap.path)
        return FileState.DELETING
    if obs is None:
        return FileState.DELETED
    if obs.hash != expected_hash or obs.size != snap.file_size:
        return FileState.UNKNOWN
    return None


def _still_owned(snap: CopySnapshot, *, expected_hash: str | None = None) -> bool:
    """Whether the delete still owns its copy at the generation it started from.

    Under the series lock (a short transaction), the copy must still hold the
    delete's token, be ``deleting`` at the expected generation and its file be
    live. A takeover by a repair, a storage URL change or any other reset
    makes this false.
    """
    ctx = get_context()
    try:
        with ctx.write_session() as session:
            tr.lock_copy(
                session,
                snap.copy_id,
                token=snap.token,
                generation=snap.generation,
                states={FileState.DELETING},
                expected_hash=expected_hash,
            )
    except (tr.StaleOperation, tr.CopyBusy) as e:
        logger.warning(
            "delete: %s in %s is no longer owned by this delete (%s); it is left in the store",
            snap.path,
            snap.storage_location,
            e,
        )
        return False
    return True


def delete_object(
    store: StorageLocation,
    snap: CopySnapshot,
    *,
    expected_hash: str | None = None,
    before_delete: Callable[[], None] | None = None,
) -> FileState:
    """Delete one copy's object, only if it is still what hp last observed (STO-13).

    The object's current ``info()`` must match the copy's last observation:
    size, mtime (when one was recorded) and, when both sides have one, the
    backend-reported md5 against the verified hash. A copy hp has never
    observed is never deleted from the store. Immediately before ``rm`` the
    delete re-confirms, under the series lock, that it still owns the copy
    (token, generation, ``deleting``) and checks the object again, so a delete
    that was taken over or reset (a repair, a storage URL change) removes
    nothing. The check-to-``rm`` gap that remains cannot be closed on stores
    without a conditional delete (see docs/design/operations.md).

    Args:
        store: the copy's storage location.
        snap: the copy, owned by the delete.
        expected_hash: also read back the source and guard the logical hash
            immediately before a transfer/rule removes it.
        before_delete: optional final guard (a move checks that its verified
            destination remains available). A refused guard raises.

    Returns:
        ``deleted`` when the object is gone (deleted now or already absent);
        ``unknown`` when it does not match, was never observed, or the delete
        no longer owns the copy, and was left in the store; ``deleting`` when
        the store delete failed.
    """
    if (outcome := _unchanged(store, snap, expected_hash=expected_hash)) is not None:
        return outcome
    if not _still_owned(snap, expected_hash=expected_hash):
        return FileState.UNKNOWN
    if (outcome := _unchanged(store, snap, expected_hash=expected_hash)) is not None:
        return outcome
    if before_delete is not None:
        before_delete()
    try:
        store.rm(snap.path)
    except FileNotFoundError:
        pass
    except Exception:
        logger.exception(
            "delete: deleting %s from %s failed; the copy stays deleting",
            snap.path,
            snap.storage_location,
        )
        return FileState.DELETING
    logger.info(
        "deleted %s from storage location %s (%d bytes)",
        snap.path,
        snap.storage_location,
        snap.file_size,
    )
    return FileState.DELETED


def delete_copies(
    ctx: AppContext,
    project: str,
    copies: list[CopySnapshot],
    *,
    verify_hash: bool = False,
) -> dict[int, FileState]:
    """Run :func:`delete_object` for each owned copy; never raises on store errors.

    A copy whose storage location is not in the loaded config stays
    ``deleting``. Repairs set ``verify_hash`` to retain the source-content
    guard of an interrupted move even when size and mtime were preserved.
    """
    outcomes: dict[int, FileState] = {}
    for snap in copies:
        try:
            store = store_for(ctx, project, snap.storage_location)
        except LookupError as e:
            logger.error("delete: %s; the copy stays deleting", e)
            outcomes[snap.copy_id] = FileState.DELETING
            continue
        outcomes[snap.copy_id] = (
            delete_object(store, snap, expected_hash=snap.hash)
            if verify_hash
            else delete_object(store, snap)
        )
    return outcomes


def _begin(ctx: AppContext, file_id: int, user: str | None) -> tuple[str, list[CopySnapshot]]:
    """Own the file's copies, repairing expired uploads on them first (at most once each)."""
    from heliopuddle.server.services.upload import repair_expired

    repaired: set[int] = set()
    while True:
        try:
            with ctx.write_session() as session:
                return models.begin_delete(
                    session, file_id, updated_by=user, lease=ctx.settings.op_lease
                )
        except models.NeedsRepair as e:
            if e.copy_id in repaired:
                raise models.CopyBusy(e.copy_id, None) from e
            repaired.add(e.copy_id)
            repair_expired(e.copy_id)


@task(name="delete_file", task_run_name="delete_file-{file_id}")
def delete_file(file_id: int, user: str | None = None) -> FileState:
    """Delete every copy of a live file from its store, then soft-delete the file.

    1. Guarded: every copy becomes ``deleting`` with its own token (an expired
       upload on a copy is repaired first).
    2. Outside any transaction, each object is deleted only if it matches the
       copy's last observation (:func:`delete_object`).
    3. Guarded finish: copies ``deleted``, ``unknown`` (left in the store) or
       ``deleting`` without a token (the store delete failed; retried later);
       the file is soft-deleted when every copy is ``deleted``; latest is
       recomputed.

    Args:
        file_id: the file to remove.
        user: user id recorded as ``updated_by``.

    Returns:
        ``deleted`` when the file was removed; else ``deleting`` when a store
        delete did not finish, or ``unknown`` when a copy was left in the
        store because it did not match.

    Raises:
        NotFoundError: if the file does not exist or is already deleted.
        CopyBusy: another operation owns one of its copies.
    """
    ctx = get_context()
    project, copies = _begin(ctx, file_id, user)
    outcomes = delete_copies(ctx, project, copies)
    with ctx.write_session() as session:
        file = models.finish_delete(session, file_id, copies, outcomes, updated_by=user)
        deleted = file.deleted_at is not None
        path = file.path
    if deleted:
        logger.info(
            "delete_file: file %s (%s/%s) deleted from %d storage location(s)",
            file_id,
            project,
            path,
            len(copies),
        )
        return FileState.DELETED
    if FileState.DELETING in outcomes.values():
        logger.warning(
            "delete_file: file %s (%s/%s) not fully deleted; will be retried",
            file_id,
            project,
            path,
        )
        return FileState.DELETING
    logger.warning(
        "delete_file: file %s (%s/%s) left in the store: copies did not match",
        file_id,
        project,
        path,
    )
    return FileState.UNKNOWN


@flow(name="delete_files")
def delete_files(file_ids: list[int], user: str | None = None) -> dict[int, FileState]:
    """Run :func:`delete_file` for each file.

    Every file is attempted; if any task fails, or leaves a file not
    ``deleted``, the flow fails after the rest have run, naming those ids.

    Args:
        file_ids: the files to remove.
        user: user id recorded as ``updated_by``.

    Returns:
        The final state of each file, keyed by id.

    Raises:
        RuntimeError: if removing any file failed or did not finish.
    """
    results: dict[int, FileState] = {}
    failed: list[int] = []
    for file_id in file_ids:
        state = delete_file(file_id, user, return_state=True)
        if state.is_completed():
            results[file_id] = state.result()
            if results[file_id] != FileState.DELETED:
                logger.error("delete_file(%s) left the file %s", file_id, results[file_id])
                failed.append(file_id)
        else:
            logger.error("delete_file(%s) failed: %s", file_id, state.message)
            failed.append(file_id)
    logger.info("delete_files: %d of %d file(s) deleted", len(results) - len(failed), len(file_ids))
    if failed:
        raise RuntimeError(f"delete_file failed for: {', '.join(map(str, failed))}")
    return results

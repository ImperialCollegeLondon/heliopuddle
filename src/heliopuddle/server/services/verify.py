"""Verify copies in their stores (SVC-02, SVC-11, SVC-16).

``verify_file`` is the one place that decides a copy's verified state. The API
calls it inline when a file is registered; the ``verify_files`` flow runs it
over many copies for background and bulk re-verification. See
docs/design/operations.md ("Verify").
"""

from __future__ import annotations

from prefect import flow, task

from heliopuddle.server import models
from heliopuddle.server.context import get_context
from heliopuddle.server.database.orm import File, FileStorageLocation, utcnow
from heliopuddle.server.models import transitions as tr
from heliopuddle.server.models.series import lock_series_by_id, recompute_latest
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services.run_logging import run_logger
from heliopuddle.server.services.stores import Observation, ObservationChanged, observe, store_for

logger = run_logger(__name__)

VERIFIABLE_STATES = frozenset(
    {FileState.PENDING, FileState.AVAILABLE, FileState.UNKNOWN, FileState.MISSING}
)
"""States a verify may change; owned, deleting and deleted copies are left alone."""


def _current_state(copy_id: int) -> FileState:
    with get_context().sessionmaker() as session:
        return session.get_one(FileStorageLocation, copy_id).state


@task(name="verify_file", task_run_name="verify_file-{file_storage_location_id}")
def verify_file(file_storage_location_id: int, generation: int | None = None) -> FileState:
    """Re-hash one copy in its store and record the result, if nothing changed meanwhile.

    1. Snapshot the copy: its generation (or the expected ``generation``),
       the file's hash and registered size. An owned copy, or one that is
       ``deleting``/``deleted``/``overwriting``, is left as it is.
    2. Observe the object outside any transaction: stat, md5 by reading the
       content (never a backend-reported md5), stat again. If it changed
       between the stats, the copy is left as it is.
    3. Guarded update: only if the copy's generation, the file's live state
       and hash are unchanged. Hash **and** size match → ``available`` (size,
       mtime, hash and ``last_seen_at`` recorded); otherwise ``unknown``.
       ``is_latest`` is recomputed. A stale result is dropped and logged.

    Args:
        file_storage_location_id: the copy to verify.
        generation: the generation the caller expects; a different current
            generation means the result would be stale, so nothing is done.

    Returns:
        The copy's state afterwards.

    Raises:
        NotFoundError: if the copy does not exist.
        LookupError: if its storage location is not in the loaded config.
    """
    ctx = get_context()
    with ctx.sessionmaker() as session:
        copy = models.get_copy(session, file_storage_location_id)
        if copy is None:
            raise models.NotFoundError(
                f"file storage location {file_storage_location_id} not found"
            )
        project = copy.file.project.name
        location_name = copy.storage_location.name
        path = copy.file.path
        expected = copy.file.hash.lower()
        expected_size = copy.file.size
        current = copy.state
        snapshot_generation = copy.generation
        owned = copy.op_token is not None
    if generation is not None and generation != snapshot_generation:
        logger.info(
            "verify_file(%s): generation %s, expected %s; skipped",
            file_storage_location_id,
            snapshot_generation,
            generation,
        )
        return current
    if owned or current not in VERIFIABLE_STATES:
        logger.info(
            "verify_file(%s): %s, owned=%s; skipped", file_storage_location_id, current, owned
        )
        return current

    store = store_for(ctx, project, location_name)
    try:
        obs = observe(store, path)
    except ObservationChanged as e:
        logger.info("verify_file(%s): %s; left as it is", file_storage_location_id, e)
        return current

    try:
        with ctx.write_session() as session:
            locked = tr.lock_copy(
                session,
                file_storage_location_id,
                generation=snapshot_generation,
                expected_hash=expected,
                states=VERIFIABLE_STATES,
            )
            now = utcnow()
            if obs is not None and obs.hash == expected and obs.size == expected_size:
                new_state, new_hash = FileState.AVAILABLE, obs.hash
            else:
                new_state, new_hash = FileState.UNKNOWN, None
            unchanged = (
                locked.state == new_state
                and locked.hash == new_hash
                and (obs is None or (locked.size == obs.size and locked.mtime == obs.mtime))
            )
            _log_outcome(project, location_name, path, new_state, obs, expected_size)
            if unchanged:
                # Not even last_seen_at: any write bumps updated_at (ORM onupdate), which
                # moves the incremental change key of an unchanged copy.
                return locked.state
            if obs is not None:
                locked.last_seen_at = now
            locked.state = new_state
            locked.hash = new_hash
            if obs is not None:
                locked.size = obs.size
                locked.mtime = obs.mtime
            locked.updated_at = now
            file = session.get_one(File, locked.file_id)
            recompute_latest(session, lock_series_by_id(session, file.series_id))
            return locked.state
    except (tr.StaleOperation, tr.CopyBusy) as e:
        logger.info("verify_file(%s): result dropped: %s", file_storage_location_id, e)
        return _current_state(file_storage_location_id)


def _log_outcome(
    project: str,
    location: str,
    path: str,
    new_state: FileState,
    obs: Observation | None,
    expected_size: int,
) -> None:
    if new_state == FileState.AVAILABLE:
        logger.info(
            "verified %s/%s in storage location %s: hash ok (%d bytes)",
            project,
            path,
            location,
            expected_size,
        )
    elif obs is None:
        logger.warning(
            "verify failed for %s/%s in storage location %s: the file is missing; "
            "its copy is now unknown",
            project,
            path,
            location,
        )
    else:
        logger.warning(
            "verify failed for %s/%s in storage location %s: hash or size differs from the "
            "registered content; its copy is now unknown",
            project,
            path,
            location,
        )


@flow(name="verify_files")
def verify_files(file_storage_location_ids: list[int]) -> dict[int, FileState]:
    """Run :func:`verify_file` for each copy.

    Every copy is attempted; if any task fails, the flow fails after the rest
    have run, naming the failed ids.

    Args:
        file_storage_location_ids: the copies to verify.

    Returns:
        The state of each copy, keyed by id.

    Raises:
        RuntimeError: if verifying any copy failed.
    """
    results: dict[int, FileState] = {}
    failed: list[int] = []
    for copy_id in file_storage_location_ids:
        state = verify_file(copy_id, return_state=True)
        if state.is_completed():
            results[copy_id] = state.result()
        else:
            logger.error("verify_file(%s) failed: %s", copy_id, state.message)
            failed.append(copy_id)
    logger.info(
        "verify_files: %d of %d cop(ies) verified, %d failed",
        len(results),
        len(file_storage_location_ids),
        len(failed),
    )
    if failed:
        raise RuntimeError(f"verify_file failed for: {', '.join(map(str, failed))}")
    return results

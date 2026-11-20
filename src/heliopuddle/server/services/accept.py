"""Accept an ``unknown`` copy as it is now (API-11, D4). See docs/design/operations.md."""

from __future__ import annotations

from prefect import task
from prefect.cache_policies import NO_CACHE

from heliopuddle.server.context import get_context
from heliopuddle.server.models import accept as accept_db
from heliopuddle.server.models.accept import AmbiguousCopyError, NotUnknownError
from heliopuddle.server.models.files import NotFoundError
from heliopuddle.server.services.run_logging import run_logger
from heliopuddle.server.services.stores import ObservationChanged, observe, store_for

logger = run_logger(__name__)

__all__ = ["AmbiguousCopyError", "NotFoundError", "NotUnknownError", "accept_copy"]


@task(name="accept_copy", task_run_name="accept_copy-{file_id}", cache_policy=NO_CACHE)
def accept_copy(file_id: int, storage_location: str | None = None, user: str | None = None) -> None:
    """Accept a file's ``unknown`` copy as its content.

    1. Choose the copy (the one in ``storage_location``, or the file's only
       ``unknown`` copy) and read its generation.
    2. Observe it in the store outside any transaction: stat, md5 by reading
       the content (a backend-reported md5 is never trusted here), stat again.
    3. Guarded write under the series lock, only if the copy is still
       ``unknown`` at that generation: the file takes the observed hash and
       size, the copy becomes ``available``, ``updated_by`` is ``user`` and
       ``is_latest`` is recomputed. If the hash changed, the file's other
       copies become ``unknown``.

    Args:
        file_id: the file.
        storage_location: the storage location of the copy to accept.
        user: user id recorded as ``updated_by``.

    Raises:
        NotFoundError: the file does not exist or is deleted, or there is no
            such storage location in its project.
        AmbiguousCopyError: no storage location was given and the file has no,
            or several, ``unknown`` copies.
        NotUnknownError: the copy is not ``unknown``, or changed meanwhile.
        CopyBusy: another operation owns the copy.
        LookupError: the storage location is not in the loaded config, or the
            object changed while it was read.
        FileNotFoundError: the object is not in the store (the copy stays
            ``unknown``).
    """
    ctx = get_context()
    with ctx.sessionmaker() as session:
        chosen = accept_db.choose(session, file_id, storage_location)
    target = chosen.target
    store = store_for(ctx, target.project, target.storage_location)
    try:
        obs = observe(store, target.path)
    except ObservationChanged as e:
        raise LookupError(f"file {file_id}: {e}; try again") from e
    if obs is None:
        raise FileNotFoundError(target.path)
    with ctx.write_session() as session:
        accept_db.accept_copy_record(
            session, chosen, size=obs.size, mtime=obs.mtime, hash=obs.hash, user=user
        )
    logger.info(
        "accepted %s/%s in storage location %s as the content of file %s (%d bytes, hash %s)",
        target.project,
        target.path,
        target.storage_location,
        file_id,
        obs.size,
        obs.hash,
    )

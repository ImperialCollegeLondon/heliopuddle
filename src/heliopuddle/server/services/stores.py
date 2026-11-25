"""Opening storage locations and observing objects in them, for the services.

Every service opens a store through :func:`store_for`, with the URL and
storage options from the loaded project config (never the DB, so secrets stay
in the environment or in Prefect blocks, resolved by
:func:`heliopuddle.storage.credentials.open_location`). :func:`observe` is the
one consistent observation of an object (SVC-16): stat, hash by reading, stat
again.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from heliopuddle.server.context import AppContext
from heliopuddle.storage import StorageLocation
from heliopuddle.storage.credentials import open_location


def store_for(ctx: AppContext, project: str, storage_location: str) -> StorageLocation:
    """Open a configured storage location.

    Args:
        ctx: the app context.
        project: project name.
        storage_location: storage location name.

    Raises:
        LookupError: the storage location is not in the loaded config.
        StorageLocationIncomplete: a Prefect block it needs is missing (STO-18).
    """
    cfg = ctx.storage_location_config(project, storage_location)
    if cfg is None:
        raise LookupError(f"storage location {project}/{storage_location} is not in config")
    return open_location(cfg)


@dataclass(frozen=True)
class Observation:
    """One consistent observation of an object: size, mtime and md5 together."""

    size: int
    mtime: datetime | None
    hash: str


class ObservationChanged(Exception):
    """The object changed while it was being hashed; nothing can be concluded."""


def observe(store: StorageLocation, path: str) -> Observation | None:
    """Observe ``path``: stat, md5 by reading the content, stat again.

    Args:
        store: the storage location.
        path: relative path.

    Returns:
        The observation, or None if the object does not exist.

    Raises:
        ObservationChanged: size or mtime differ between the two stats, or the
            object disappeared while it was read.
    """
    try:
        before = store.info(path)
    except FileNotFoundError:
        return None
    try:
        digest = store.md5(path)
        after = store.info(path)
    except FileNotFoundError as e:
        raise ObservationChanged(f"{path} disappeared while it was read") from e
    if (before.size, before.mtime) != (after.size, after.mtime):
        raise ObservationChanged(f"{path} changed while it was read")
    return Observation(size=after.size, mtime=after.mtime, hash=digest)


__all__ = ["Observation", "ObservationChanged", "observe", "store_for"]

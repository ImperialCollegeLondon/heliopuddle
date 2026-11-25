"""Opening storage locations and observing objects in them, for the services.

Every service opens a store through :func:`store_for`, with the URL and
storage options from the loaded project config (never the DB, so secrets stay
in the environment or in Prefect blocks, resolved by
:func:`heliopuddle.storage.credentials.open_location`). :func:`observe` is the
one consistent observation of an object (SVC-16): stat, hash by reading, stat
again.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime

from heliopuddle.config import StorageLocationConfig
from heliopuddle.server import models
from heliopuddle.server.context import AppContext
from heliopuddle.storage import StorageLocation
from heliopuddle.storage.credentials import (
    StorageLocationIncomplete,
    open_location,
    resolve,
    root_identity,
)


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
    store = open_location(cfg)
    note_resolved_root(ctx, project, cfg)
    return store


_seen_roots: dict[tuple[str, str], str] = {}
_seen_lock = threading.Lock()


def note_resolved_root(ctx: AppContext, project: str, cfg: StorageLocationConfig) -> None:
    """Reset a block-configured storage location whose resolved root changed (STO-19, SVC-12).

    A storage location configured through Prefect blocks keeps its configured
    ``url`` (``prefect-block:<name>`` or a placeholder) when the block's base
    path changes, so the move is detected here instead, on every open: the
    credential-free root (:func:`root_identity`) is compared with the one
    stored in ``storage_location.resolved_root``. The first one is recorded; a
    different one resets the copies exactly as a changed ``url`` does. A
    per-process cache skips the database while the root is unchanged. Nothing
    happens for storage locations without blocks (``sync_config`` covers them)
    or while the blocks cannot be resolved.

    Args:
        ctx: the app context.
        project: project name.
        cfg: the storage location config.
    """
    if not cfg.uses_blocks:
        return
    try:
        url, _options = resolve(cfg)
    except StorageLocationIncomplete:
        return
    identity = root_identity(url)
    key = (project, cfg.name)
    with _seen_lock:
        if _seen_roots.get(key) == identity:
            return
    with ctx.write_session() as session:
        row = models.get_storage_location(session, project, cfg.name)
        if row is None:
            return
        if row.resolved_root is not None and row.resolved_root != identity:
            models.storage_location_moved(session, row, project, row.resolved_root, identity)
        row.resolved_root = identity
    with _seen_lock:
        _seen_roots[key] = identity


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


__all__ = ["Observation", "ObservationChanged", "note_resolved_root", "observe", "store_for"]

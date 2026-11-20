"""Store-side object helpers for uploads: move, remove, probe and temporary files."""

from __future__ import annotations

import contextlib
import posixpath

from heliopuddle.server.context import AppContext
from heliopuddle.server.services.run_logging import run_logger
from heliopuddle.server.services.stores import Observation, store_for
from heliopuddle.storage import StorageLocation
from heliopuddle.storage.location import parse_temp_path

logger = run_logger("heliopuddle.server.services.upload")


def _mv(store: StorageLocation, src: str, dst: str) -> None:
    store.move(src, dst)


def _rm(store: StorageLocation, path: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        store.rm(path)


def _rm_quietly(store: StorageLocation, *paths: str) -> None:
    for path in paths:
        try:
            _rm(store, path)
        except Exception:
            logger.warning("could not remove %s; the index removes it later", path)


def _replace(store: StorageLocation, src: str, dst: str) -> None:
    """Move ``src`` to ``dst``, removing ``dst`` first (only used to repair)."""
    _rm(store, dst)
    _mv(store, src, dst)


def _present(store: StorageLocation, path: str) -> bool:
    """True if ``path`` exists; only "not found" means absent (other errors are raised)."""
    try:
        store.info(path)
    except FileNotFoundError:
        return False
    return True


def object_present(ctx: AppContext, project: str, storage_location: str, path: str) -> bool:
    """Whether an object exists at ``path`` in a storage location, for a reservation.

    An unreadable store counts as present, so a reservation stages its write
    rather than risk writing over an object it could not see; a storage
    location that is not configured counts as absent (the reservation then
    fails on its own).
    """
    try:
        store = store_for(ctx, project, storage_location)
    except LookupError:
        return False
    try:
        return _present(store, path)
    except Exception:
        logger.warning("reserve: could not look for %s; its write is staged", path)
        return True


def _move_into_place(store: StorageLocation, final: str, staged: str, backup: str) -> None:
    """Publish ``staged`` at ``final`` (steps 2 of "Overwrite"); the original is never lost.

    Where the backend's move replaces its destination: ``mv(staged, final)``.
    Otherwise an existing ``final`` is first moved to ``backup`` and restored
    if the second move fails.
    """
    if store.move_replaces or not _present(store, final):
        _mv(store, staged, final)
        return
    _mv(store, final, backup)
    try:
        _mv(store, staged, final)
    except Exception:
        if not store.exists(final):
            _mv(store, backup, final)
        raise


def _matches(obs: Observation | None, hash: str | None, size: int | None) -> bool:
    return obs is not None and hash is not None and obs.hash == hash and obs.size == size


def temp_files(store: StorageLocation, path: str) -> tuple[list[str], list[str]]:
    """Return the staging and backup files of ``path``.

    Only the exact shapes ``<path>.<uuid>.hp-tmp`` and
    ``<path>.<uuid>.hp-old.hp-tmp`` match, so the temporary files of a longer
    sibling path (``<path>.v2.<uuid>.hp-tmp``) are never taken for this path's.
    Only the owner of a path writes these, so any found while repairing that
    path's copy belong to an abandoned operation.

    Returns:
        ``(staged, backups)`` as relative paths.
    """
    directory = posixpath.dirname(path)
    basename = posixpath.basename(path)
    try:
        names = store.fs.ls(store.full_path(directory) if directory else store.root, detail=False)
    except FileNotFoundError:
        return [], []
    staged: list[str] = []
    backups: list[str] = []
    for name in names:
        base = posixpath.basename(str(name).rstrip("/"))
        parsed = parse_temp_path(base)
        if parsed is None or parsed.name != basename:
            continue
        rel = posixpath.join(directory, base) if directory else base
        (backups if parsed.backup else staged).append(rel)
    return sorted(staged), sorted(backups)

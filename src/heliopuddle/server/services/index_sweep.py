"""The full index's sweep of orphaned staging and backup files (STO-10).

Staging (``<path>.<token>.hp-tmp``) and backup (``<path>.<token>.hp-old.hp-tmp``)
files belong to an upload or overwrite. While a copy holds an operation token
(live, or expired and awaiting repair) every such file at its path is left
alone: the operation, or its repair, owns them. A file whose path has no
token-holding copy is an orphan, for instance a backup whose removal failed
after a successful publish, or a staged file a client wrote after its
reservation expired. docs/design/operations.md ("Expiry and repair") says what
becomes of it: a backup is restored if the final object is missing or does not
match the recorded hash, else removed; a staged file is removed.

The check (copy has no token) and the store action are not atomic: an
operation that reserves the path in between is not seen. Its files carry a new
token, so only a restore of an old backup can race it, and the settle time
(files written recently are not swept) makes that window negligible.
"""

from __future__ import annotations

import contextlib
from typing import Literal

from heliopuddle.server.context import AppContext
from heliopuddle.server.models import index as index_db
from heliopuddle.server.services.run_logging import run_logger
from heliopuddle.server.services.stores import ObservationChanged, observe
from heliopuddle.storage import StorageLocation
from heliopuddle.storage.location import parse_temp_path

logger = run_logger(__name__)

SweepOutcome = Literal["removed", "restored", "left"]


def sweep_temp_file(
    ctx: AppContext,
    store: StorageLocation,
    *,
    project_id: int,
    storage_location_id: int,
    path: str,
) -> SweepOutcome:
    """Settle one listed ``*.hp-tmp`` file that may be an orphan.

    Args:
        ctx: the app context.
        store: the storage location the file was listed in.
        project_id: the project.
        storage_location_id: the storage location.
        path: the temporary file's relative path.

    Returns:
        ``removed`` (a staged file, or a backup that is not needed),
        ``restored`` (a backup moved back to the final path) or ``left``
        (not an hp temporary file, owned by an operation, or it could not be
        judged).
    """
    parsed = parse_temp_path(path)
    if parsed is None:
        return "left"
    final = parsed.name
    with ctx.sessionmaker() as session:
        target = index_db.get_index_target(session, project_id, storage_location_id, final)
    if target is not None and target.op_token is not None:
        return "left"
    if not parsed.backup:
        _rm(store, path)
        return "removed"
    if target is None:
        _rm(store, path)  # the file is gone: nothing to restore the original for
        return "removed"
    try:
        current = observe(store, final)
    except ObservationChanged:
        return "left"
    if current is not None and current.hash == target.file_hash.lower():
        _rm(store, path)
        return "removed"
    logger.warning("index: restoring the original of %s from its backup %s", final, path)
    _rm(store, final)
    store.move(path, final)
    return "restored"


def _rm(store: StorageLocation, path: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        store.rm(path)

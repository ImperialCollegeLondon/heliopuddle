"""DB access for the index flows and accept (SVC-03, SVC-08, SVC-10, API-11).

These functions never touch a store: the services list and hash outside any
transaction, then call these in one write transaction per file. Each write
locks the file's series row first (as every writer does) and recomputes
``is_latest``. See docs/design/indexing.md.

Split by responsibility: :mod:`.targets` (what the DB holds), :mod:`.copies`
(record observations), :mod:`.removal` (gone copies, reset), :mod:`.progress`
(progress, checkpoints, incremental candidates) and :mod:`.accept`.
"""

from heliopuddle.server.models.index.accept import (
    AcceptTarget,
    AmbiguousCopyError,
    NotUnknownError,
    accept_copy_record,
    available_copy_ids,
    get_accept_target,
)
from heliopuddle.server.models.index.copies import (
    index_add_file,
    index_set_copy,
    touch_copies,
)
from heliopuddle.server.models.index.progress import (
    IndexChange,
    IndexCheckpoint,
    get_index_progress,
    incremental_candidates,
    index_checkpoint,
    record_index_progress,
    save_index_checkpoint,
)
from heliopuddle.server.models.index.removal import (
    RemovalCandidate,
    RemoveOutcome,
    copy_awaits_repair,
    index_remove_copy,
    removal_candidates,
    reset_observations,
    stale_copies,
)
from heliopuddle.server.models.index.targets import (
    IndexTarget,
    get_index_target,
    get_index_targets,
    lock_index_target,
)

__all__ = [
    "AcceptTarget",
    "AmbiguousCopyError",
    "IndexChange",
    "IndexCheckpoint",
    "IndexTarget",
    "NotUnknownError",
    "RemovalCandidate",
    "RemoveOutcome",
    "accept_copy_record",
    "available_copy_ids",
    "copy_awaits_repair",
    "get_accept_target",
    "get_index_progress",
    "get_index_target",
    "get_index_targets",
    "incremental_candidates",
    "index_add_file",
    "index_checkpoint",
    "index_remove_copy",
    "index_set_copy",
    "lock_index_target",
    "record_index_progress",
    "removal_candidates",
    "reset_observations",
    "save_index_checkpoint",
    "stale_copies",
    "touch_copies",
]

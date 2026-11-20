"""DB access functions. Each takes a SQLAlchemy ``Session``; callers commit.

These functions own row locking. They never touch a store; store-side work
belongs to :mod:`heliopuddle.server.services`. Write functions expect a session
from ``write_session()`` (``BEGIN IMMEDIATE`` on SQLite).
"""

from heliopuddle.server.models.files import (
    ConflictError,
    InactiveError,
    NotFoundError,
    RegisterOutcome,
    RegisterResult,
    VersionImmutable,
    begin_delete,
    finish_delete,
    get_copy,
    get_file,
    get_file_by_path,
    like_escape,
    list_files,
    list_versions,
    lock_path,
    register_file,
    set_content_date,
    set_copy_state,
    set_copy_verified,
    set_rules_ignored,
    to_file_read,
    update_metadata,
    validate_path,
)
from heliopuddle.server.models.index import (
    AmbiguousCopyError,
    NotUnknownError,
)
from heliopuddle.server.models.projects import (
    get_index_progress,
    get_project,
    get_storage_location,
    list_storage_locations,
    sync_config,
)
from heliopuddle.server.models.search import (
    SearchPage,
    project_summary,
    search_files,
    search_words,
)
from heliopuddle.server.models.series import (
    VersionKindMismatch,
    claim_version_type,
    lock_series,
    lock_series_by_id,
    lock_series_of_file,
    recompute_latest,
    sort_versions,
    version_key,
)
from heliopuddle.server.models.snapshots import CopySnapshot
from heliopuddle.server.models.transitions import CopyBusy, NeedsRepair, StaleOperation

__all__ = [
    "AmbiguousCopyError",
    "ConflictError",
    "CopyBusy",
    "CopySnapshot",
    "InactiveError",
    "NeedsRepair",
    "NotFoundError",
    "NotUnknownError",
    "RegisterOutcome",
    "RegisterResult",
    "SearchPage",
    "StaleOperation",
    "VersionImmutable",
    "VersionKindMismatch",
    "begin_delete",
    "claim_version_type",
    "finish_delete",
    "get_copy",
    "get_file",
    "get_file_by_path",
    "get_index_progress",
    "get_project",
    "get_storage_location",
    "like_escape",
    "list_files",
    "list_storage_locations",
    "list_versions",
    "lock_path",
    "lock_series",
    "lock_series_by_id",
    "lock_series_of_file",
    "project_summary",
    "recompute_latest",
    "register_file",
    "search_files",
    "search_words",
    "set_content_date",
    "set_copy_state",
    "set_copy_verified",
    "set_rules_ignored",
    "sort_versions",
    "sync_config",
    "to_file_read",
    "update_metadata",
    "validate_path",
    "version_key",
]

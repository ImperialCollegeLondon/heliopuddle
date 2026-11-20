"""Files and copies: register, check, query, metadata and removal (API-01/02/07/08/09).

Split by responsibility; this package re-exports the public names:

- :mod:`.errors`: the errors and version wording.
- :mod:`.register`: path validation, ``lock_path`` and ``register_file``.
- :mod:`.query`: reads (by id or path, filtered pages, a series' versions).
- :mod:`.update`: metadata, content date and rule exclusion.
- :mod:`.delete`: the DB half of deleting a file.
- :mod:`.copies`: a file's copies and its API schema.
"""

from heliopuddle.server.models.files.copies import (
    get_copy,
    set_copy_state,
    set_copy_verified,
    to_file_read,
)
from heliopuddle.server.models.files.delete import (
    DEFAULT_LEASE,
    begin_delete,
    finish_delete,
)
from heliopuddle.server.models.files.errors import (
    ConflictError,
    InactiveError,
    NotFoundError,
    VersionImmutable,
    describe_version,
)
from heliopuddle.server.models.files.query import _LIKE_ESCAPE as _LIKE_ESCAPE
from heliopuddle.server.models.files.query import (
    Order,
    get_file,
    get_file_by_path,
    like_escape,
    list_files,
    list_versions,
    project_name_of_file,
    project_name_of_series,
)
from heliopuddle.server.models.files.query import _file_options as _file_options
from heliopuddle.server.models.files.register import (
    RegisterOutcome,
    RegisterResult,
    Target,
    create_file,
    locate,
    lock_file_copy,
    lock_path,
    register_file,
    resolve_target,
    validate_path,
)
from heliopuddle.server.models.files.update import (
    set_content_date,
    set_rules_ignored,
    update_metadata,
)

__all__ = [
    "DEFAULT_LEASE",
    "ConflictError",
    "InactiveError",
    "NotFoundError",
    "Order",
    "RegisterOutcome",
    "RegisterResult",
    "Target",
    "VersionImmutable",
    "begin_delete",
    "create_file",
    "describe_version",
    "finish_delete",
    "get_copy",
    "get_file",
    "get_file_by_path",
    "like_escape",
    "list_files",
    "list_versions",
    "locate",
    "lock_file_copy",
    "lock_path",
    "project_name_of_file",
    "project_name_of_series",
    "register_file",
    "resolve_target",
    "set_content_date",
    "set_copy_state",
    "set_copy_verified",
    "set_rules_ignored",
    "to_file_read",
    "update_metadata",
    "validate_path",
]

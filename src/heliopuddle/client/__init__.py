"""heliopuddle client: talks to the API over HTTP and to the store with fsspec.

Must never import the database layer (enforced by import-linter, CLI-04).
"""

from heliopuddle.client._sync import SyncAction, SyncResult
from heliopuddle.client.auth import LoginResult
from heliopuddle.client.client import (
    BusyError,
    ConflictError,
    HelioPuddleClient,
    IntegrityError,
)
from heliopuddle.client.errors import (
    AuthenticationError,
    HelioPuddleError,
    NotFoundError,
    PermissionDeniedError,
    SyncError,
)
from heliopuddle.config.models import RuleSpec
from heliopuddle.server.schemas import (
    AuthConfig,
    FilePreview,
    Me,
    ProjectSummary,
    RuleRead,
    RuleTriggerResult,
    RunInfo,
    SearchResults,
    StorageLocationSummary,
    UserRead,
    ViewCounts,
)
from heliopuddle.server.schemas import FileRead as FileRecord
from heliopuddle.server.schemas.preview import Cell, PreviewColumn, PreviewTable, PreviewVariable
from heliopuddle.storage.hashing import file_digest

__all__ = [
    "AuthConfig",
    "AuthenticationError",
    "BusyError",
    "Cell",
    "ConflictError",
    "FilePreview",
    "FileRecord",
    "HelioPuddleClient",
    "HelioPuddleError",
    "IntegrityError",
    "LoginResult",
    "Me",
    "NotFoundError",
    "PermissionDeniedError",
    "PreviewColumn",
    "PreviewTable",
    "PreviewVariable",
    "ProjectSummary",
    "RuleRead",
    "RuleSpec",
    "RuleTriggerResult",
    "RunInfo",
    "SearchResults",
    "StorageLocationSummary",
    "SyncAction",
    "SyncError",
    "SyncResult",
    "UserRead",
    "ViewCounts",
    "file_digest",
]

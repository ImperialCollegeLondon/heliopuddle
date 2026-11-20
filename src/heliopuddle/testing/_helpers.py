"""Module-level helpers of the in-memory fake: version checks, hashing, filters."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from heliopuddle.client import ConflictError, FileRecord, HelioPuddleError
from heliopuddle.globs import glob_match
from heliopuddle.server.schemas import FileState, FileStorageLocationRead, VersionOverride
from heliopuddle.storage.location import TEMP_SUFFIX

_CHUNK = 1024 * 1024

LATEST_ELIGIBLE = frozenset({FileState.AVAILABLE, FileState.OVERWRITING})
"""Copy states that make a live file eligible for latest (the server's rule, API-12)."""


@dataclass(frozen=True)
class Call:
    """One recorded client call: the method name and the path (or id) it was about."""

    method: str
    target: str | int | None


Version = tuple[int | None, str | None]


def parse_version(version_number: int | None, version_text: str | None) -> Version:
    """Check a version override as the real client does, before any request (API-20).

    Raises:
        ValueError: both are given, or (a pydantic ``ValidationError``, as the
            real client raises when it builds the request) a number out of
            0..2^63-1 or a text that is empty or longer than 255 characters.
    """
    if version_number is not None and version_text is not None:
        raise ValueError("give either version_number or version_text, not both")
    override = VersionOverride(version_number=version_number, version_text=version_text)
    return override.version_number, override.version_text


def version_kind(version: Version) -> str | None:
    if version[0] is not None:
        return "number"
    return "text" if version[1] is not None else None


def check_same_version(existing: FileRecord, version: Version) -> None:
    """A live file's version never changes: a different override is a ``409`` (API-22)."""
    if version == (None, None):
        return
    if version != (existing.version_number, existing.version_text):
        raise ConflictError(
            f"the file at {existing.path!r} has version {label(existing)}; "
            "a version cannot be changed",
            existing,
        )


def label(record: FileRecord) -> str:
    if record.version_number is not None:
        return f"v{record.version_number}"
    return record.version_text if record.version_text is not None else "none"


def validate_path(path: str) -> None:
    if not path or path.startswith("/") or "\\" in path:
        raise HelioPuddleError(422, f"path must be relative and '/'-separated: {path!r}")
    if any(part in {"", ".", ".."} for part in path.split("/")):
        raise HelioPuddleError(422, f"path must not contain empty, '.' or '..' segments: {path!r}")
    if path.endswith(TEMP_SUFFIX):
        raise HelioPuddleError(422, f"path must not end with {TEMP_SUFFIX}: {path!r}")


def copy_of(record: FileRecord, location: str) -> FileStorageLocationRead | None:
    return next((c for c in record.storage_locations if c.storage_location == location), None)


def md5(path: Path) -> tuple[str, int]:
    digest = hashlib.md5(usedforsecurity=False)
    size = 0
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def mtime(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, UTC)


def aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def in_range(value: datetime | None, start: datetime | None, end: datetime | None) -> bool:
    if start is None and end is None:
        return True
    if value is None:
        return False
    if start is not None and value < start.replace(tzinfo=start.tzinfo or UTC):
        return False
    return end is None or value <= end.replace(tzinfo=end.tzinfo or UTC)


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class FindFilter:
    """The filters of ``find``, with ``state`` already normalised to a set of strings."""

    path: str | None = None
    latest_only: bool = False
    states: frozenset[str] | None = None
    storage_location: str | None = None
    content_date_from: datetime | None = None
    content_date_to: datetime | None = None
    include_deleted: bool = False
    series_id: int | None = None
    deleted_only: bool = False
    ignored_from_all_rules: bool | None = None


def _status_matches(record: FileRecord, f: FindFilter) -> bool:
    deleted = record.deleted_at is not None
    if f.deleted_only and not deleted:
        return False
    if deleted and not (f.include_deleted or f.deleted_only):
        return False
    return record.is_latest or not f.latest_only


def _copies_match(record: FileRecord, f: FindFilter) -> bool:
    copies = [
        c
        for c in record.storage_locations
        if f.storage_location is None or c.storage_location == f.storage_location
    ]
    if f.storage_location is not None and not any(c.state != FileState.DELETED for c in copies):
        return False
    return f.states is None or any(str(c.state) in f.states for c in copies)


def matches(record: FileRecord, f: FindFilter) -> bool:
    """Whether ``record`` passes every filter of ``find``."""
    if not _status_matches(record, f):
        return False
    if f.path is not None and not glob_match(record.path, f.path):
        return False
    if f.series_id is not None and record.series_id != f.series_id:
        return False
    if not _copies_match(record, f):
        return False
    if not in_range(record.content_date, f.content_date_from, f.content_date_to):
        return False
    return f.ignored_from_all_rules in (None, record.ignored_from_all_rules)

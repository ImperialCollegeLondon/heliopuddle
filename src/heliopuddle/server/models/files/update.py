"""Change a live file's metadata, content date or rule exclusion (API-07, API-19)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from heliopuddle.config.models import DEFAULT_VERSION_RULES, VersionRule
from heliopuddle.server.database.orm import File, utcnow
from heliopuddle.server.models.files.errors import NotFoundError
from heliopuddle.versioning import InvalidVersionError, derive


def update_metadata(
    session: Session,
    file_id: int,
    patch: Mapping[str, Any],
    *,
    replace: bool = False,
    updated_by: str | None = None,
) -> File:
    """Merge ``patch`` into a live file's metadata, or replace it.

    A shallow merge: each key of ``patch`` is set, and a ``None`` value removes
    the key. With ``replace`` the metadata becomes ``patch`` (without its
    ``None`` values). Sets ``updated_by`` and ``updated_at``.

    Args:
        session: a session in a write transaction; the caller commits.
        file_id: the file.
        patch: keys to set (``None`` removes).
        replace: replace the whole object instead of merging.
        updated_by: user id from the request header.

    Returns:
        The file.

    Raises:
        NotFoundError: if the file does not exist or is deleted.
    """
    file = session.scalar(select(File).where(File.id == file_id).with_for_update())
    if file is None or file.deleted_at is not None:
        raise NotFoundError(f"file {file_id} not found")
    merged: dict[str, Any] = {} if replace else dict(file.file_metadata or {})
    for key, value in patch.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    file.file_metadata = merged
    file.updated_by = updated_by
    file.updated_at = utcnow()
    session.flush()
    return file


def set_content_date(
    session: Session,
    file_id: int,
    content_date: datetime | None,
    *,
    rules: Sequence[VersionRule] = DEFAULT_VERSION_RULES,
    updated_by: str | None = None,
) -> File:
    """Set a live file's content date, or reset it to the date in its path (API-19).

    Args:
        session: a write transaction; the caller commits before notifying rules.
        file_id: the logical file.
        content_date: the new date (a naive time is UTC). None goes back to the
            date the project's version ``rules`` take from the path, or none.
        rules: the project's version rules (default: the default rule; empty:
            none, so unversioned).
        updated_by: the requesting user.

    Raises:
        NotFoundError: the file is absent or deleted.
    """
    file = session.scalar(select(File).where(File.id == file_id).with_for_update())
    if file is None or file.deleted_at is not None:
        raise NotFoundError(f"file {file_id} not found")
    if content_date is None:
        try:
            content_date = derive(file.path, rules).content_date
        except InvalidVersionError:
            content_date = None  # the file was indexed as unversioned
    elif content_date.tzinfo is None:
        content_date = content_date.replace(tzinfo=UTC)
    file.content_date = content_date
    file.updated_by = updated_by
    file.updated_at = utcnow()
    session.flush()
    return file


def set_rules_ignored(
    session: Session, file_id: int, ignored: bool, *, updated_by: str | None = None
) -> File:
    """Set a live file's rule exclusion, recording the user and change timestamp.

    Args:
        session: a write transaction; the caller commits before notifying rules.
        file_id: the logical file.
        ignored: whether every rule should ignore the file.
        updated_by: the requesting user.

    Raises:
        NotFoundError: the file is absent or deleted.
    """
    file = session.scalar(select(File).where(File.id == file_id).with_for_update())
    if file is None or file.deleted_at is not None:
        raise NotFoundError(f"file {file_id} not found")
    file.ignored_from_all_rules = ignored
    file.updated_by = updated_by
    file.updated_at = utcnow()
    session.flush()
    return file

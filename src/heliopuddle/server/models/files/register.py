"""Register a file already written to a storage location (API-01, API-20..22).

Series and versions are derived from the path with the project's version rules
(:func:`heliopuddle.versioning.derive`); ``is_latest`` is recomputed under the
series row lock by :func:`heliopuddle.server.models.series.recompute_latest`.
The helpers here (:func:`resolve_target`, :func:`create_file`,
:func:`lock_file_copy`) are shared with the upload protocol's reserve step.
"""

from __future__ import annotations

import posixpath
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from heliopuddle.config.models import DEFAULT_VERSION_RULES, VersionRule
from heliopuddle.server.database.orm import (
    File,
    FileStorageLocation,
    Project,
    Series,
    StorageLocation,
    utcnow,
)
from heliopuddle.server.models import transitions
from heliopuddle.server.models.files.errors import (
    ConflictError,
    InactiveError,
    NotFoundError,
    VersionImmutable,
    describe_version,
)
from heliopuddle.server.models.projects import get_project, get_storage_location
from heliopuddle.server.models.series import (
    claim_version_type,
    lock_series,
    lock_series_by_id,
    recompute_latest,
)
from heliopuddle.server.models.transitions import bump, check_free
from heliopuddle.server.schemas import FileState
from heliopuddle.storage.location import TEMP_SUFFIX
from heliopuddle.versioning import VersionInfo, apply_override, derive


def lock_path(
    session: Session,
    project_id: int,
    path: str,
    rules: Sequence[VersionRule],
    *,
    version_number: int | None = None,
    version_text: str | None = None,
) -> tuple[Series, File | None, VersionInfo]:
    """Lock the series of ``path`` and find its live file (register and reserve).

    The series and version are derived from ``path`` with ``rules``. For a new
    path the override, if any, replaces the derived version (API-20) and the
    version's kind is checked against the series (API-21). A live file keeps
    its series and version: an override must equal its version (API-22).

    Args:
        session: a session in a write transaction.
        project_id: the project.
        path: the relative path.
        rules: the project's version rules (empty: none, so unversioned).
        version_number: a number version override.
        version_text: a text version override.

    Returns:
        ``(series, file, info)``: the locked series, the live file or None, and
        the version of a new file.

    Raises:
        InvalidVersionError: a rule extracted an invalid version for a new
            file without an override.
        ValueError: both overrides were given.
        VersionKindMismatch: a new file's version is of the other kind than
            its series.
        VersionImmutable: an override differs from the live file's version.
    """
    # The series comes from the path; the rules' version is only needed (and
    # only validated) for a new file without an override.
    info = derive(path, rules, version=False)
    series = lock_series(session, project_id, info)
    file = session.scalar(
        select(File).where(
            File.project_id == project_id, File.path == path, File.deleted_at.is_(None)
        )
    )
    if file is None:
        if version_number is None and version_text is None:
            info = derive(path, rules)
        else:
            info = apply_override(info, version_number, version_text)
        claim_version_type(series, info)
        return series, None, info
    if file.series_id != series.id:
        # The rules changed since the file was added; it keeps its series.
        series = lock_series_by_id(session, file.series_id)
    if (version_number is not None or version_text is not None) and (
        version_number,
        version_text,
    ) != (file.version_number, file.version_text):
        raise VersionImmutable(
            file.id,
            path,
            describe_version(file.version_number, file.version_text),
            describe_version(version_number, version_text),
        )
    return series, file, info


class RegisterOutcome(StrEnum):
    """What a register did.

    ``created``: a new file row (a new path, or a path whose file was deleted).
    ``unchanged``: the live file at the path already has this hash.

    Overwriting goes through the upload protocol
    (:mod:`heliopuddle.server.models.uploads`), never through a register.
    """

    CREATED = "created"
    UNCHANGED = "unchanged"


@dataclass(frozen=True)
class RegisterResult:
    """Result of :func:`register_file`.

    Attributes:
        file_id: the file.
        copy_id: its copy in the storage location registered to.
        outcome: what happened.
        needs_verify: True when the copy was (re)set to ``pending`` and should
            be verified.
        generation: the copy's generation after the register (a verify
            applies only while it is unchanged).
    """

    file_id: int
    copy_id: int
    outcome: RegisterOutcome
    needs_verify: bool
    generation: int = 0


def validate_path(path: str) -> str:
    """Check that ``path`` is a canonical relative ``/``-separated path.

    Args:
        path: the path relative to the storage location root.

    Returns:
        ``path`` unchanged.

    Raises:
        ValueError: if it is empty, absolute, uses ``\\``, has empty, ``.`` or
            ``..`` segments, ends with ``/`` or is a temp file (``*.hp-tmp``).
    """
    if not path or path.startswith("/") or "\\" in path:
        raise ValueError(f"path must be relative and '/'-separated: {path!r}")
    if any(part in {"", ".", ".."} for part in path.split("/")):
        raise ValueError(f"path must not contain empty, '.' or '..' segments: {path!r}")
    if path.endswith(TEMP_SUFFIX):
        raise ValueError(f"path must not end with {TEMP_SUFFIX}: {path!r}")
    return path


def resolve_target(
    session: Session, project: str, storage_location: str
) -> tuple[Project, StorageLocation]:
    """Find the project and an active storage location a file is written to.

    Raises:
        NotFoundError: the project or storage location does not exist.
        InactiveError: the storage location is no longer in config.
    """
    project_row = get_project(session, project)
    if project_row is None:
        raise NotFoundError(f"project {project!r} not found")
    location = get_storage_location(session, project, storage_location)
    if location is None:
        raise NotFoundError(f"storage location {storage_location!r} not found in {project!r}")
    if not location.active:
        raise InactiveError(f"storage location {storage_location!r} is not active")
    return project_row, location


@dataclass(frozen=True)
class Target:
    """What a write to ``path`` in a storage location works on (see :func:`locate`).

    Attributes:
        project: the project row.
        location: the active storage location.
        series: the path's series, locked.
        file: the live file at the path, or None for a new file.
        info: the version of a new file.
    """

    project: Project
    location: StorageLocation
    series: Series
    file: File | None
    info: VersionInfo


def locate(
    session: Session,
    project: str,
    storage_location: str,
    path: str,
    rules: Sequence[VersionRule],
    version_number: int | None,
    version_text: str | None,
) -> Target:
    """Find the project and storage location of a write and lock the path's series.

    Raises:
        NotFoundError: the project or storage location does not exist.
        InactiveError: the storage location is no longer in config.
        (and the errors of :func:`lock_path`)
    """
    project_row, location = resolve_target(session, project, storage_location)
    series, file, info = lock_path(
        session,
        project_row.id,
        path,
        rules,
        version_number=version_number,
        version_text=version_text,
    )
    return Target(project_row, location, series, file, info)


def create_file(
    session: Session,
    target: Target,
    path: str,
    content_date: datetime | None,
    size: int,
    hash: str,
    hash_algo: str,
    metadata: dict[str, Any] | None,
    created_by: str | None,
) -> File:
    """Add and flush a new file row in ``target``'s series (``is_latest`` is the caller's)."""
    file = File(
        project_id=target.project.id,
        path=path,
        name=posixpath.basename(path),
        series_id=target.series.id,
        version_number=target.info.version_number,
        version_text=target.info.version_text,
        content_date=content_date or target.info.content_date,
        size=size,
        hash=hash,
        hash_algo=hash_algo,
        file_metadata=metadata,
        is_latest=False,
        created_by=created_by,
    )
    session.add(file)
    session.flush()
    return file


def lock_file_copy(
    session: Session, file: File, location: StorageLocation
) -> FileStorageLocation | None:
    """Lock the file's copy in ``location`` (``FOR UPDATE``), or return None."""
    return session.scalar(
        select(FileStorageLocation)
        .where(
            FileStorageLocation.file_id == file.id,
            FileStorageLocation.storage_location_id == location.id,
        )
        .with_for_update()
    )


def register_file(
    session: Session,
    *,
    project: str,
    storage_location: str,
    path: str,
    hash: str,
    size: int,
    metadata: dict[str, Any] | None = None,
    created_by: str | None = None,
    hash_algo: str = "md5",
    rules: Sequence[VersionRule] = DEFAULT_VERSION_RULES,
    content_date: datetime | None = None,
    version_number: int | None = None,
    version_text: str | None = None,
) -> RegisterResult:
    """Register a file already written to a storage location (see docs/design/operations.md).

    This is the upload protocol's reserve and complete in one transaction
    (``POST /projects/{p}/files``), for a new path or the same hash only. The
    series and version are derived from ``path`` with ``rules`` (the
    project's version rules; empty means unversioned), and a new
    file takes the version override if one is given (see :func:`lock_path`).
    The series row is upserted and locked, then, at a live ``path``:

    - a copy owned by a live operation: :class:`CopyBusy`; one holding an
      expired operation or an unfinished delete: :class:`NeedsRepair`;
    - same hash: nothing changes (``unchanged``); the copy is reset to
      ``pending`` (generation bumped) only if it is not ``available``, or
      created if missing;
    - different hash: :class:`ConflictError` (overwrites use the upload
      protocol).

    Otherwise a new file row is created with a ``pending`` copy (``created``).
    In every case ``is_latest`` is recomputed for the series in this
    transaction.

    Args:
        session: a session in a write transaction; the caller commits.
        project: project name.
        storage_location: storage location name.
        path: relative path in the store.
        hash: hex digest of the content (stored lower-case).
        size: size in bytes.
        metadata: client metadata.
        created_by: user id from the request header (``updated_by`` on overwrite).
        hash_algo: digest algorithm.
        rules: the project's version rules (default: the default rule; empty:
            none, so unversioned).
        content_date: overrides a date from the rule's ``date`` group.
        version_number: a number version for a new file (API-20).
        version_text: a text version for a new file (API-20).

    Returns:
        The file, its copy and what happened.

    Raises:
        ValueError: if ``path`` is invalid or both overrides are given.
        InvalidVersionError: if a rule extracted an invalid version.
        VersionKindMismatch: a new file's version is of the other kind than
            its series (API-21).
        VersionImmutable: an override differs from the live file's version
            (API-22).
        NotFoundError: if the project or storage location does not exist.
        InactiveError: if the storage location is inactive.
        CopyBusy: if another live operation owns the copy.
        NeedsRepair: if the copy holds an expired operation or a failed delete.
        ConflictError: if a live file at ``path`` has a different hash.
    """
    validate_path(path)
    hash = hash.lower()
    target = locate(session, project, storage_location, path, rules, version_number, version_text)
    file, location, series = target.file, target.location, target.series
    if file is None:
        file = create_file(
            session, target, path, content_date, size, hash, hash_algo, metadata, created_by
        )
        outcome = RegisterOutcome.CREATED
    else:
        outcome = RegisterOutcome.UNCHANGED

    copy = lock_file_copy(session, file, location)
    if copy is not None:
        check_free(copy, transitions.clock())
    if file.hash != hash:
        raise ConflictError(file.id, path)
    needs_verify = True
    if copy is None:
        copy = FileStorageLocation(file_id=file.id, storage_location_id=location.id, generation=0)
        session.add(copy)
    elif outcome == RegisterOutcome.UNCHANGED and copy.state == FileState.AVAILABLE:
        needs_verify = False
    else:
        bump(copy)
    if needs_verify:
        copy.state = FileState.PENDING
        copy.size = None
        copy.mtime = None
        copy.hash = None
        copy.updated_at = utcnow()
    session.flush()
    recompute_latest(session, series)
    return RegisterResult(
        file_id=file.id,
        copy_id=copy.id,
        outcome=outcome,
        needs_verify=needs_verify,
        generation=copy.generation,
    )

"""Reserve an upload under the series lock (step 2 of "Add a file")."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from heliopuddle.config.models import DEFAULT_VERSION_RULES, VersionRule
from heliopuddle.server.database.orm import FileStorageLocation
from heliopuddle.server.models import transitions as tr
from heliopuddle.server.models.files import (
    ConflictError,
    create_file,
    locate,
    lock_file_copy,
    validate_path,
)
from heliopuddle.server.models.series import recompute_latest
from heliopuddle.server.models.uploads.types import (
    NotOverwritable,
    Reservation,
    new_token,
    write_path,
)
from heliopuddle.server.schemas import FileState
from heliopuddle.storage.location import staging_path


def _set_pending(copy: FileStorageLocation, hash: str, size: int) -> None:
    copy.pending_hash = hash
    copy.pending_size = size


def reserve(
    session: Session,
    *,
    project: str,
    storage_location: str,
    path: str,
    hash: str,
    size: int,
    lease: timedelta,
    metadata: dict[str, Any] | None = None,
    created_by: str | None = None,
    rules: Sequence[VersionRule] = DEFAULT_VERSION_RULES,
    content_date: datetime | None = None,
    overwrite: bool = False,
    hash_algo: str = "md5",
    final_exists: bool = False,
    version_number: int | None = None,
    version_text: str | None = None,
) -> Reservation:
    """Decide an upload under the series lock (step 2 of "Add a file").

    - No live file at ``path``: a new file row (series and version from the
      rules, or the version override; see
      :func:`heliopuddle.server.models.files.lock_path`) and a ``pending``
      copy owned by a new token; write the final path.
    - Same hash and the copy is ``available``: ``unchanged``.
    - Same hash otherwise (no copy here, or ``pending``, ``unknown``,
      ``missing``, ``deleted``): the copy is reserved (``pending`` with a
      token); write the final path.
    - Different hash without ``overwrite``: :class:`ConflictError`.
    - A ``new`` reservation with ``final_exists`` gets ``write_path`` =
      ``<path>.<token>.hp-tmp`` and ``pending_staged`` on its copy.
    - Different hash with ``overwrite``: the ``available`` copy becomes
      ``overwriting`` with a token and ``pending_hash``/``pending_size``; write
      the staging path ``<path>.<token>.hp-tmp``. ``metadata``,
      ``content_date`` and ``updated_by`` are kept on the copy and applied only when it publishes.

    Args:
        session: a session in a write transaction; the caller commits.
        project: project name.
        storage_location: storage location name.
        path: relative path.
        hash: hex md5 of the content to write.
        size: its size.
        lease: how long the reservation lasts before it expires.
        metadata: client metadata.
        created_by: user id (``updated_by`` on overwrite).
        rules: the project's version rules (default: the default rule; empty:
            none, so unversioned).
        content_date: overrides a date from the rule's ``date`` group.
        overwrite: replace an ``available`` copy that has a different hash.
        hash_algo: digest algorithm.
        final_exists: an object already exists at ``path`` in the storage
            location (the caller looked, outside the transaction). A ``new``
            reservation then writes a unique staging path and publishes it at
            complete, so the existing object is never written over (it is
            replaced only by a verified publish, and never removed by an
            abort or repair).
        version_number: a number version for a new file (API-20).
        version_text: a text version for a new file (API-20).

    Returns:
        The reservation.

    Raises:
        ValueError: if ``path`` is invalid or both overrides are given.
        InvalidVersionError: if a rule extracted an invalid version.
        VersionKindMismatch: a new file's version is of the other kind than
            its series (API-21).
        VersionImmutable: an override differs from the live file's version
            (API-22), also with ``overwrite``.
        NotFoundError: unknown project or storage location.
        InactiveError: the storage location is inactive.
        CopyBusy: another live operation owns the copy.
        NeedsRepair: the copy holds an expired operation or a failed delete.
        ConflictError: a different hash without ``overwrite``.
        NotOverwritable: ``overwrite`` of a copy that is not ``available``.
    """
    validate_path(path)
    hash = hash.lower()
    target = locate(session, project, storage_location, path, rules, version_number, version_text)
    file, location, series = target.file, target.location, target.series
    now = tr.clock()
    token = new_token()
    deadline = now + lease

    if file is None:
        file = create_file(
            session, target, path, content_date, size, hash, hash_algo, metadata, created_by
        )
        copy = FileStorageLocation(file_id=file.id, storage_location_id=location.id, generation=0)
        session.add(copy)
        tr.take(copy, FileState.PENDING, token, deadline)
        _set_pending(copy, hash, size)
        copy.pending_staged = final_exists
        session.flush()
        return Reservation(
            "new", file.id, copy.id, token, write_path(path, token, final_exists), deadline
        )

    copy = lock_file_copy(session, file, location)
    if copy is not None:
        tr.check_free(copy, now)

    if file.hash == hash:
        if copy is not None and copy.state == FileState.AVAILABLE:
            return Reservation("unchanged", file.id, copy.id)
        if copy is None:
            copy = FileStorageLocation(
                file_id=file.id, storage_location_id=location.id, generation=0
            )
            session.add(copy)
        else:
            tr.bump(copy)
        tr.take(copy, FileState.PENDING, token, deadline)
        _set_pending(copy, hash, size)
        copy.pending_staged = final_exists
        session.flush()
        recompute_latest(session, series)
        return Reservation(
            "new", file.id, copy.id, token, write_path(path, token, final_exists), deadline
        )

    if not overwrite:
        raise ConflictError(file.id, path)
    if copy is None or copy.state != FileState.AVAILABLE:
        raise NotOverwritable(file.id, path, copy.state if copy is not None else None)
    tr.take(copy, FileState.OVERWRITING, token, deadline)
    _set_pending(copy, hash, size)
    # Applied to the file only if the overwrite publishes (finish_overwrite).
    copy.pending_metadata = metadata
    copy.pending_content_date = content_date
    copy.pending_by = created_by
    session.flush()
    return Reservation("overwrite", file.id, copy.id, token, staging_path(path, token), deadline)

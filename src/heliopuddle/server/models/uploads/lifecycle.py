"""Operations on a client's upload token: look up, renew, claim, late complete."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from heliopuddle.server.database.orm import File, FileStorageLocation, Project
from heliopuddle.server.models import transitions as tr
from heliopuddle.server.models.snapshots import CopySnapshot, copy_snapshot
from heliopuddle.server.models.uploads.types import (
    Reservation,
    TokenNotCurrent,
    new_token,
    write_path,
)
from heliopuddle.server.schemas import FileState, UploadMode


def _owned_copy_id(session: Session, token: str) -> int:
    copy_id = session.scalar(
        select(FileStorageLocation.id).where(FileStorageLocation.op_token == token)
    )
    if copy_id is None:
        raise TokenNotCurrent(f"upload {token} is not current")
    return copy_id


_UPLOAD_STATES = frozenset({FileState.PENDING, FileState.OVERWRITING})


def _lock_upload(session: Session, token: str) -> FileStorageLocation:
    copy_id = _owned_copy_id(session, token)
    try:
        return tr.lock_copy(session, copy_id, token=token, states=_UPLOAD_STATES)
    except (tr.StaleOperation, tr.CopyBusy) as e:
        raise TokenNotCurrent(f"upload {token} is not current") from e


def project_name_of_upload(session: Session, token: str) -> str | None:
    """Return the name of the project an upload token belongs to, or None.

    Matches a token that still owns its copy, and one whose content a repair
    published (so a late ``complete`` can still be authorized).
    """
    return session.scalar(
        select(Project.name)
        .join(File, File.project_id == Project.id)
        .join(FileStorageLocation, FileStorageLocation.file_id == File.id)
        .where(
            (FileStorageLocation.op_token == token) | (FileStorageLocation.published_token == token)
        )
        .limit(1)
    )


def renew(session: Session, token: str, lease: timedelta) -> Reservation:
    """Extend an upload's deadline to now + ``lease``.

    An expired reservation that nobody has taken over can still be renewed.

    Raises:
        TokenNotCurrent: the token is unknown, finished or was taken over.
    """
    copy = _lock_upload(session, token)
    copy.op_deadline = tr.clock() + lease
    file = session.get_one(File, copy.file_id)
    mode: UploadMode = "overwrite" if copy.state == FileState.OVERWRITING else "new"
    where = write_path(file.path, token, mode == "overwrite" or copy.pending_staged)
    return Reservation(mode, file.id, copy.id, token, where, copy.op_deadline)


def content_expectations(session: Session, token: str, lease: timedelta) -> tuple[str, int]:
    """Renew a current reservation and return its expected content hash and size."""
    copy = _lock_upload(session, token)
    if copy.pending_hash is None or copy.pending_size is None:
        raise TokenNotCurrent("the upload no longer has pending content")
    copy.op_deadline = tr.clock() + lease
    return copy.pending_hash, copy.pending_size


def claim(session: Session, token: str, lease: timedelta, *, staged: bool = False) -> CopySnapshot:
    """Take an upload over from the client to complete or abort it.

    The copy gets a fresh token and deadline, so the client's token stops
    being current: a second complete, a renew or an abort racing this one gets
    :class:`TokenNotCurrent`. ``pending_hash``, ``state`` and ``generation``
    are kept. The staging path is named after the *client's* token, which
    the returned snapshot no longer carries, so callers keep it (a later
    repair finds staging files by name pattern instead).

    Args:
        session: a write session.
        token: the client's upload token.
        lease: the claim's lease.
        staged: force a staging write, including for a new upload via the API.

    Raises:
        TokenNotCurrent: the token is unknown, finished or was taken over.
    """
    copy = _lock_upload(session, token)
    if staged:
        copy.pending_staged = True
    copy.op_token = new_token()
    copy.op_deadline = tr.clock() + lease
    session.flush()
    return copy_snapshot(session, copy)


def published_by_repair(session: Session, token: str) -> int | None:
    """Return the file id if a repair published the content reserved under ``token``.

    A client whose reservation expired may still call complete after the
    repair adopted what it wrote. That repair recorded the client's token on
    the copy (``published_token``); while the copy is still ``available``
    with no operation and no content-changing transition since, the late
    complete is answered with the record instead of a conflict.

    Returns:
        The file id, or None.
    """
    row = session.execute(
        select(FileStorageLocation.file_id, File.deleted_at)
        .join(File, File.id == FileStorageLocation.file_id)
        .where(
            FileStorageLocation.published_token == token,
            FileStorageLocation.state == FileState.AVAILABLE,
            FileStorageLocation.op_token.is_(None),
        )
    ).first()
    if row is None or row[1] is not None:
        return None
    return int(row[0])

"""Types and errors of the upload protocol: reservations, tokens, write paths."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from heliopuddle.server.schemas import FileState, UploadMode
from heliopuddle.storage.location import staging_path


class TokenNotCurrent(LookupError):
    """The upload token is unknown, finished, or was taken over (``409``)."""


class NotOverwritable(Exception):
    """``overwrite`` was asked but the copy in that storage location is not ``available``.

    Attributes:
        file_id: the live file at the path.
    """

    def __init__(self, file_id: int, path: str, state: FileState | None) -> None:
        what = state.value if state is not None else "absent"
        super().__init__(
            f"cannot overwrite {path!r}: its copy in this storage location is {what}, "
            "not available (accept or remove the file first)"
        )
        self.file_id = file_id


@dataclass(frozen=True)
class Reservation:
    """Answer to :func:`reserve`.

    Attributes:
        mode: ``unchanged`` (nothing to write), ``new`` (write ``write_path``:
            the final path, or a staging path when an object already exists
            there) or ``overwrite`` (write ``write_path``, a staging path).
        file_id: the file.
        copy_id: its copy in the storage location.
        token: the operation's token (None for ``unchanged``).
        write_path: where the client writes (None for ``unchanged``).
        deadline: when ownership expires (None for ``unchanged``).
    """

    mode: UploadMode
    file_id: int
    copy_id: int
    token: str | None = None
    write_path: str | None = None
    deadline: datetime | None = None


def new_token() -> str:
    """A new operation token (a UUID)."""
    return str(uuid.uuid4())


def write_path(path: str, token: str, staged: bool) -> str:
    return staging_path(path, token) if staged else path

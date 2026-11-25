"""Paths, temporary names and records of a storage location (no I/O).

Split out of :mod:`heliopuddle.storage.location`, which re-exports every name.
"""

from __future__ import annotations

import io
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import BinaryIO, Literal

from heliopuddle.storage.redact import redact_text, redact_url

TEMP_SUFFIX = ".hp-tmp"

CreateRoot = bool | Literal["auto"]


class CheckedReader(io.RawIOBase):
    """Check operation ownership before and after each read from a binary source."""

    def __init__(self, source: BinaryIO, check: Callable[[], None]) -> None:
        self.source = source
        self.check = check

    def read(self, size: int = -1) -> bytes:
        self.check()
        data = self.source.read(size)
        self.check()
        return data


def staging_path(path: str, token: str) -> str:
    """Unique staging path for an operation: ``<path>.<token>.hp-tmp``."""
    return f"{path}.{token}{TEMP_SUFFIX}"


def backup_path(path: str, token: str) -> str:
    """Backup path used while publishing an overwrite: ``<path>.<token>.hp-old.hp-tmp``."""
    return f"{path}.{token}.hp-old{TEMP_SUFFIX}"


@dataclass(frozen=True)
class TempName:
    """A parsed ``<name>.<token>.hp-tmp`` or ``<name>.<token>.hp-old.hp-tmp`` file name.

    Attributes:
        name: the final name (or path) the temporary file belongs to.
        token: the operation token (a UUID) in the name.
        backup: True for a backup (``.hp-old.hp-tmp``), False for a staged file.
    """

    name: str
    token: str
    backup: bool


def parse_temp_path(path: str) -> TempName | None:
    """Parse a staging or backup path made by :func:`staging_path` or :func:`backup_path`.

    The token must be a canonical UUID, so a file such as
    ``data.dat.v2.<token>.hp-tmp`` is never read as a temporary file of
    ``data.dat``.

    Args:
        path: a relative path or a bare file name.

    Returns:
        The parts, or None if ``path`` does not have exactly that shape.
    """
    if not path.endswith(TEMP_SUFFIX):
        return None
    rest = path[: -len(TEMP_SUFFIX)]
    backup = rest.endswith(".hp-old")
    if backup:
        rest = rest[: -len(".hp-old")]
    name, dot, token = rest.rpartition(".")
    if not dot or not name:
        return None
    try:
        if str(uuid.UUID(token)) != token:
            return None
    except ValueError:
        return None
    return TempName(name, token, backup)


class SymlinkError(ValueError):
    """A path goes through a symbolic link, which a storage location never follows (STO-15)."""


class StorageRootMissingError(FileNotFoundError):
    """The root of a storage location (its bucket, container, share or directory) is missing.

    Raised when the root does not exist and ``create_root`` does not allow
    creating it, or creating it did not work.
    """

    def __init__(self, url: str, reason: str | None = None) -> None:
        super().__init__(
            f"the root of storage location {redact_url(url)!r} does not exist: "
            + (
                redact_text(reason)
                if reason
                else "create the bucket, container, share or directory, or set create_root: true"
            )
        )
        self.url = redact_url(url)


@dataclass(frozen=True)
class ListEntry:
    """One file found by :meth:`StorageLocation.list_files`.

    Attributes:
        path: relative, ``/``-separated path within the storage location.
        size: size in bytes.
        mtime: modification time (timezone-aware), if the backend reports one.
        md5: the backend's own md5 (hex) if it reports one, else None.
    """

    path: str
    size: int
    mtime: datetime | None
    md5: str | None = None


@dataclass(frozen=True)
class FileInfo:
    """Size and modification time of a file in a storage location."""

    size: int
    mtime: datetime | None


class PathOutsideStorageLocation(ValueError):
    """A project path that a storage location with a ``path_prefix`` cannot hold (STO-23)."""


_GLOB_CHARS = frozenset("*?[]{}")


def normalise_path_prefix(prefix: str | None) -> str:
    """Return a storage location's ``path_prefix`` in canonical form (``""``: whole project).

    The prefix is the project folder that the storage location's root holds,
    e.g. ``upload_mirror`` or ``data/mirror``: relative, ``/``-separated, one
    or more plain segments. Leading and trailing ``/`` are dropped.

    Raises:
        ValueError: an absolute path, a ``\\``, an empty, ``.`` or ``..``
            segment, or a glob character.
    """
    if not prefix:
        return ""
    if prefix.startswith("/") or "\\" in prefix:
        raise ValueError(f"path_prefix must be a relative '/'-separated folder: {prefix!r}")
    text = prefix.strip("/")
    segments = text.split("/")
    if not text or any(seg in ("", ".", "..") for seg in segments):
        raise ValueError(f"path_prefix has an empty, '.' or '..' segment: {prefix!r}")
    if _GLOB_CHARS & set(text):
        raise ValueError(f"path_prefix may not contain glob characters: {prefix!r}")
    return text


def strip_path_prefix(path_prefix: str, path: str) -> str:
    """Map a normalised project ``path`` to its path within a storage location.

    Args:
        path_prefix: the storage location's normalised ``path_prefix``.
        path: a normalised relative project path.

    Raises:
        PathOutsideStorageLocation: ``path`` is not below ``path_prefix``.
    """
    if not path_prefix:
        return path
    if not path.startswith(path_prefix + "/"):
        raise PathOutsideStorageLocation(
            f"path {path!r} is outside this storage location, which holds only "
            f"{path_prefix + '/'!r}"
        )
    return path[len(path_prefix) + 1 :]


def listing_scope(path_prefix: str, prefix: str) -> str | None:
    """Where to list for the project listing ``prefix``, relative to the root.

    Returns ``""`` for the whole root, a relative folder within it, or None
    when ``prefix`` lies outside the storage location (nothing to list).
    """
    if not path_prefix:
        return prefix
    if not prefix or path_prefix == prefix or path_prefix.startswith(prefix + "/"):
        return ""  # the listing covers the whole storage location
    if prefix.startswith(path_prefix + "/"):
        return prefix[len(path_prefix) + 1 :]
    return None

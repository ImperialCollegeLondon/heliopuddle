"""Everything hp knows about particular fsspec backends. See docs/design/storage.md.

hp stores files through the generic fsspec filesystem API (``open``, ``info``,
``find``, ``exists``, ``rm``, ``mv``, ``makedirs``), so any fsspec backend can
be a storage location. A few facts cannot be had from that API and differ per
backend; this module holds all of them, so supporting a new backend never
needs a change anywhere else (at most a line here):

1. **Which protocols are object stores** (:data:`OBJECT_STORE_PROTOCOLS`). On
   object stores directories are implicit, so only the bucket or container
   (the first path segment of the root) has to exist (:func:`root_anchor`),
   and a missing one is not created by default.
2. **How to read a backend-reported md5** from ``info`` metadata
   (:func:`native_md5`), used only as an indexing shortcut.
3. **Which ``info`` keys hold a modification time** (:func:`info_mtime`).
4. **Whether ``mv`` replaces an existing destination** (:func:`move_replaces`),
   used by the overwrite publish step.
5. **How to write so that a failed source never commits** (:func:`begin_write`,
   :func:`finish_write`, :func:`abort_write`). Buffered object-store writers
   (``fsspec.spec.AbstractBufferedFile``) only publish an object when they are
   closed, so their commit is deferred and a failed write is discarded instead
   of closed. Other file objects write in place, so the caller removes a
   partial new file.
6. **Native free space** (:func:`free_space`), including remote volume queries.
7. **Safe same-filesystem copies** (:func:`same_filesystem`), based on the
   connection identity and an implemented generic copy operation.
8. **Whether a path is a local file path** (:func:`local_path`), for readers
   that need a real file (file previews of CDF files).

No other module in ``heliopuddle`` may name a protocol, a backend package or a
backend's metadata key; ``tests/unit/test_backends.py`` scans the source tree
to enforce this.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import shutil
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, cast

import fsspec
from fsspec.spec import AbstractBufferedFile

__all__ = [
    "NON_REPLACING_MOVE_PROTOCOLS",
    "OBJECT_STORE_PROTOCOLS",
    "abort_write",
    "begin_write",
    "finish_write",
    "free_space",
    "info_mtime",
    "is_deferred",
    "is_link",
    "is_object_store",
    "local_path",
    "move_replaces",
    "native_md5",
    "path_has_symlink",
    "root_anchor",
    "same_filesystem",
]

#: fsspec protocols of object stores (S3 and S3-compatible, GCS, Azure Blob and
#: ADLS, Alibaba OSS, Cloudflare R2).
OBJECT_STORE_PROTOCOLS: frozenset[str] = frozenset(
    {"s3", "s3a", "gs", "gcs", "az", "abfs", "abfss", "adl", "oss", "r2"}
)

# info keys that hold a modification time, in order of preference: generic
# ``mtime`` (local, SFTP, SMB, memory, GCS), Azure ``last_modified``, S3
# ``LastModified``, then a creation time where nothing else is reported.
_MTIME_KEYS = ("mtime", "last_modified", "LastModified", "updated", "created")

_HEX = frozenset("0123456789abcdef")


def _protocols(protocols: str | tuple[str, ...] | list[str]) -> tuple[str, ...]:
    return (protocols,) if isinstance(protocols, str) else tuple(protocols)


def is_object_store(protocols: str | tuple[str, ...] | list[str]) -> bool:
    """Return True if any of ``protocols`` (``fs.protocol``) is an object store.

    Args:
        protocols: a protocol name or a filesystem's tuple of protocol names.
    """
    return any(p.lower() in OBJECT_STORE_PROTOCOLS for p in _protocols(protocols))


NON_REPLACING_MOVE_PROTOCOLS: frozenset[str] = frozenset({"smb", "sftp", "ssh"})
"""Backends whose rename does not replace an existing destination."""


def move_replaces(protocols: str | tuple[str, ...] | list[str]) -> bool:
    """Whether ``fs.mv(src, dst)`` replaces an existing ``dst`` on this backend.

    Object stores (a move is copy then delete) and local/memory filesystems
    replace; SMB and SFTP renames fail when the destination exists, so the
    overwrite publish step backs up the original first (docs/design/operations.md).
    """
    return not any(p in NON_REPLACING_MOVE_PROTOCOLS for p in _protocols(protocols))


def free_space(fs: fsspec.AbstractFileSystem, path: str) -> int | None:
    """Return native bytes available to this connection, or None when unlimited.

    Local storage uses ``disk_usage`` on the nearest existing ancestor. SMB's
    volume query respects the caller's server-side quota; SFTP uses OpenSSH's
    ``statvfs`` extension and its unprivileged available-block count. Object
    stores and unknown protocols have no native limit. A supported backend's
    query failure is raised, never mistaken for unlimited space.

    Args:
        fs: the connected filesystem.
        path: a filesystem path (normally the storage location root).
    """
    protocols = set(_protocols(fs.protocol))
    if protocols & {"file", "local"}:
        current = Path(path).absolute()
        while True:
            try:
                return shutil.disk_usage(current).free
            except FileNotFoundError:
                if current == current.parent:
                    raise
                current = current.parent
    if "smb" in protocols:
        import smbclient

        connection: Any = fs
        share_path = path.lstrip("/\\").replace("/", "\\")
        unc = f"\\\\{connection.host}\\{share_path}"
        volume = smbclient.stat_volume(unc, port=connection.port or 445)
        return int(volume.caller_available_size)
    if protocols & {"sftp", "ssh"}:
        from paramiko.sftp import CMD_EXTENDED, CMD_EXTENDED_REPLY

        connection = fs
        kind, message = connection.ftp._request(CMD_EXTENDED, "statvfs@openssh.com", path)
        if kind != CMD_EXTENDED_REPLY or len(message.get_remainder()) < 11 * 8:
            raise OSError("invalid statvfs@openssh.com response")
        block_size, fragment_size, _blocks, _free, available = (
            message.get_int64() for _ in range(5)
        )
        return int((fragment_size or block_size) * available)
    return None


def local_path(fs: fsspec.AbstractFileSystem, path: str) -> str | None:
    """Return a local file path for ``path`` on ``fs``, or None when it has none.

    Only the local filesystem has one; every remote filesystem (and the
    in-memory one) returns None, so a caller that needs a real file copies the
    content first.

    Args:
        fs: the connected filesystem.
        path: a filesystem path on ``fs`` (no protocol).
    """
    if set(_protocols(fs.protocol)) & {"file", "local"}:
        return str(Path(path))
    return None


def is_link(fs: fsspec.AbstractFileSystem, info: Mapping[str, Any]) -> bool:
    """Whether a listing entry (``fs.ls(detail=True)`` item) is a symbolic link.

    Only the local filesystem has symbolic links that point outside a storage
    location's root; every other backend returns False.
    """
    return local_path(fs, "") is not None and bool(info.get("islink"))


def path_has_symlink(fs: fsspec.AbstractFileSystem, root: str, path: str) -> bool:
    """Whether ``path`` is, or lies below, a symbolic link inside ``root``.

    A planted link inside a local storage location could otherwise point at a
    file outside it. The root itself may be a link (an operator's choice); only
    the components below it are checked. Remote backends return False.

    Args:
        fs: the connected filesystem.
        root: the storage location's root, a filesystem path on ``fs``.
        path: a filesystem path on ``fs`` at or below ``root``.
    """
    if local_path(fs, "") is None:
        return False
    base = Path(root)
    try:
        relative = Path(path).relative_to(base)
    except ValueError:
        return True  # not below the root at all
    current = base
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def same_filesystem(
    source_fs: fsspec.AbstractFileSystem, destination_fs: fsspec.AbstractFileSystem
) -> bool:
    """Whether generic ``fs.copy`` can safely address both filesystem paths.

    Require the same implementation and fsspec connection identity, not merely
    the same protocol (two accounts or servers must stream). Also require an
    implemented ``copy`` or ``cp_file``; SFTP's inherited implementation has no
    server-side copy and therefore streams even on one connection.
    """
    cls = type(source_fs)
    if cls is not type(destination_fs):
        return False
    base = fsspec.AbstractFileSystem
    if cls.copy is base.copy and cls.cp_file is base.cp_file:
        return False
    if source_fs is destination_fs:
        return True
    identity = getattr(source_fs, "_fs_token_", None)
    return identity is not None and identity == getattr(destination_fs, "_fs_token_", None)


def begin_write(fs: fsspec.AbstractFileSystem, full_path: str) -> IO[bytes]:
    """Open ``full_path`` for writing so that nothing is published before :func:`finish_write`.

    A buffered writer (``AbstractBufferedFile``: the object-store writers) has
    its ``autocommit`` turned off before any byte is written, so the object
    appears only when :func:`finish_write` commits it. Any other file object
    (local, memory, SFTP, SMB) writes in place at ``full_path``; it is not
    opened with ``autocommit=False`` because those backends would then write
    to a temporary name of their own choosing, outside hp's ``.hp-tmp``
    convention.

    Args:
        fs: the filesystem.
        full_path: the filesystem path to write.

    Returns:
        The open binary file object.
    """
    fobj = fs.open(full_path, "wb")
    if isinstance(fobj, AbstractBufferedFile):
        fobj.autocommit = False
    return cast(IO[bytes], fobj)


def is_deferred(fobj: IO[bytes]) -> bool:
    """True if ``fobj`` publishes nothing until :func:`finish_write` (see :func:`begin_write`)."""
    return isinstance(fobj, AbstractBufferedFile) and not fobj.autocommit


def finish_write(fobj: IO[bytes]) -> None:
    """Close ``fobj`` and commit it if its commit was deferred by :func:`begin_write`.

    Raises:
        Exception: whatever the backend raises while flushing or committing;
            the caller then calls :func:`abort_write`.
    """
    deferred = is_deferred(fobj)
    fobj.close()
    if deferred:
        cast(AbstractBufferedFile, fobj).commit()


def abort_write(fobj: IO[bytes]) -> None:
    """Abandon a write whose source or commit failed, without publishing it. Never raises.

    For a buffered writer: ``discard()`` (aborts a multipart or resumable
    upload where the backend has one), then the file object is marked closed
    and its buffer dropped, so neither ``close()`` nor its finaliser can
    commit. Blocks staged by a backend without a ``discard`` are never
    committed and expire on the service side. Any other file object is
    closed to release its handle; the partial file it wrote in place is the
    caller's to remove.

    Args:
        fobj: the file object returned by :func:`begin_write`.
    """
    if isinstance(fobj, AbstractBufferedFile):
        buffered: Any = fobj
        with contextlib.suppress(Exception):
            buffered.discard()
        # Disarm: close() and __del__ commit only a file that is not closed.
        buffered.closed = True
        buffered.buffer = None
        return
    with contextlib.suppress(Exception):
        fobj.close()


def root_anchor(protocols: str | tuple[str, ...] | list[str], root: str) -> str:
    """The part of a storage location root that must already exist.

    On an object store only the bucket or container (the first path segment)
    exists as such; prefixes below it are implicit. Everywhere else the whole
    root directory must exist.

    Args:
        protocols: the filesystem's protocol(s).
        root: the root path as fsspec strips it (no protocol).
    """
    if is_object_store(protocols):
        return root.strip("/").split("/", 1)[0]
    return root


def _md5_hex(value: Any) -> str | None:
    """Normalise an md5 given as 16 raw bytes, 32 hex digits or base64, else None."""
    if isinstance(value, bytes | bytearray):
        return bytes(value).hex() if len(value) == 16 else None
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().strip('"')
    if len(text) == 32 and set(text.lower()) <= _HEX:
        return text.lower()
    try:
        raw = base64.b64decode(text, validate=True)
    except (ValueError, binascii.Error):
        return None
    return raw.hex() if len(raw) == 16 else None


def native_md5(info: Mapping[str, Any]) -> str | None:
    """Return a backend-reported md5 from fsspec ``info`` metadata as lowercase hex.

    Recognised, in order:

    - a generic ``md5`` key (hex or base64);
    - Azure ``content_md5`` or ``content_settings.content_md5`` (raw bytes,
      base64 or hex; present only for single-shot uploads);
    - GCS ``md5Hash`` (base64; absent for composite objects);
    - an S3 single-part upload's ``ETag`` (32 hex digits; multipart ETags
      contain ``-`` and are not md5s), only when the info looks like an S3
      listing (it has ``StorageClass``).

    The result is metadata any writer can set, so it is only an indexing
    shortcut: verify and accept always read the content.

    Args:
        info: an ``fs.info`` result or a ``fs.find(..., detail=True)`` value.

    Returns:
        The md5 as 32 lowercase hex digits, or None if the backend reports none.
    """
    settings = info.get("content_settings")
    if isinstance(settings, Mapping):
        nested: Any = settings.get("content_md5")  # pyright: ignore[reportUnknownMemberType]
    else:
        nested = getattr(settings, "content_md5", None)
    for value in (info.get("md5"), info.get("content_md5"), nested, info.get("md5Hash")):
        digest = _md5_hex(value)
        if digest is not None:
            return digest
    etag = str(info.get("ETag") or "").strip().strip('"').lower()
    if "StorageClass" in info and len(etag) == 32 and set(etag) <= _HEX:
        return etag
    return None


def info_mtime(info: Mapping[str, Any]) -> datetime | None:
    """Return the modification time from fsspec ``info`` metadata, timezone-aware.

    Accepts a ``datetime`` (naive means UTC), a POSIX timestamp or an ISO 8601
    string, under the first of the known time keys that is present.

    Args:
        info: an ``fs.info`` result or a ``fs.find(..., detail=True)`` value.

    Returns:
        The time, or None if the backend reports none.
    """
    for key in _MTIME_KEYS:
        value = info.get(key)
        if value is None or value == "":
            continue
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=UTC)
        if isinstance(value, int | float):
            return datetime.fromtimestamp(value, UTC)
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None

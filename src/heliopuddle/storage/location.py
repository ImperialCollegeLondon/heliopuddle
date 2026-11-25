"""An fsspec-backed storage location. See docs/design/storage.md.

This module uses only the generic fsspec filesystem API. Anything that differs
per backend is in :mod:`heliopuddle.storage.backends`.
"""

from __future__ import annotations

import contextlib
import posixpath
import shutil
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any, BinaryIO, cast

import fsspec
from fsspec.core import url_to_fs

from heliopuddle.storage import backends
from heliopuddle.storage.hashing import CHUNK_SIZE, md5_stream
from heliopuddle.storage.paths import (
    TEMP_SUFFIX,
    CheckedReader,
    CreateRoot,
    FileInfo,
    ListEntry,
    PathOutsideStorageLocation,
    StorageRootMissingError,
    SymlinkError,
    TempName,
    backup_path,
    listing_scope,
    normalise_path_prefix,
    parse_temp_path,
    staging_path,
    strip_path_prefix,
)

__all__ = [
    "TEMP_SUFFIX",
    "CheckedReader",
    "CreateRoot",
    "FileInfo",
    "ListEntry",
    "PathOutsideStorageLocation",
    "StorageLocation",
    "StorageRootMissingError",
    "SymlinkError",
    "TempName",
    "backup_path",
    "normalise_path_prefix",
    "parse_temp_path",
    "staging_path",
]


class StorageLocation:
    """A root URL plus storage options, addressed with relative ``/`` paths.

    Args:
        url: fsspec URL of the root, e.g. ``file:///data`` or
            ``<protocol>://bucket/prefix``.
        storage_options: passed to the fsspec filesystem.
        create_root: create a missing root (True), never create it (False),
            or choose per backend (``"auto"``: create it except on object
            stores, whose bucket or container must already exist).
        path_prefix: the project folder this storage location's root holds
            (STO-23), e.g. ``upload_mirror``; ``""`` for the whole project.
            Every path argument and every listed path is a project path:
            ``upload_mirror/x.csv`` is stored at ``<root>/x.csv``, and a path
            outside the prefix raises :class:`PathOutsideStorageLocation`.

    Raises:
        ValueError: if ``create_root`` is not True, False or ``"auto"``, or
            ``path_prefix`` is not a plain relative folder.
    """

    def __init__(
        self,
        url: str,
        storage_options: Mapping[str, Any] | None = None,
        create_root: CreateRoot = "auto",
        path_prefix: str = "",
    ) -> None:
        if not (isinstance(create_root, bool) or create_root == "auto"):
            raise ValueError(f"create_root must be true, false or auto: {create_root!r}")
        self.path_prefix = normalise_path_prefix(path_prefix)
        self.url = url
        self.storage_options = dict(storage_options or {})
        fs, root = url_to_fs(url, **self.storage_options)
        self.fs: fsspec.AbstractFileSystem = fs
        self.root = root.rstrip("/") or "/"
        self.create_root: CreateRoot = create_root

    @property
    def protocol(self) -> str:
        """The filesystem's primary protocol, e.g. ``file``."""
        protocol = self.fs.protocol
        return protocol if isinstance(protocol, str) else protocol[0]

    def full_path(self, path: str) -> str:
        """Return the filesystem path of a relative project path.

        Raises:
            ValueError: if ``path`` is absolute, empty or escapes the root.
            PathOutsideStorageLocation: ``path`` is outside the ``path_prefix``.
        """
        if not path or path.startswith("/") or "\\" in path:
            raise ValueError(f"path must be relative and '/'-separated: {path!r}")
        normalised = posixpath.normpath(path)
        if normalised == "." or normalised.startswith(".."):
            raise ValueError(f"path escapes the storage location root: {path!r}")
        rel = strip_path_prefix(self.path_prefix, normalised)
        return posixpath.join(self.root, rel) if self.root != "/" else "/" + rel

    def _root_path(self, rel: str) -> str:
        """The filesystem path of ``rel``, a validated path relative to the root itself."""
        if not rel or rel.startswith("/") or posixpath.normpath(rel).startswith(".."):
            raise ValueError(f"path escapes the storage location root: {rel!r}")
        return posixpath.join(self.root, rel) if self.root != "/" else "/" + rel

    def covers(self, path: str) -> bool:
        """Whether the project ``path`` can have a copy here (inside the ``path_prefix``)."""
        try:
            self.full_path(path)
        except ValueError:
            return False
        return True

    def refuse_symlink(self, path: str) -> None:
        """Raise :class:`SymlinkError` if ``path`` is, or lies below, a symlink in the root."""
        if backends.path_has_symlink(self.fs, self.root, self.full_path(path)):
            raise SymlinkError(f"path goes through a symbolic link: {path!r}")

    def exists(self, path: str) -> bool:
        """Return True if ``path`` exists."""
        return self.fs.exists(self.full_path(path))

    def info(self, path: str) -> FileInfo:
        """Return the size and modification time of ``path``.

        Raises:
            FileNotFoundError: if it does not exist.
        """
        info = self.fs.info(self.full_path(path))
        return FileInfo(size=int(info["size"]), mtime=backends.info_mtime(info))

    def free_space(self, *, quota: int | None = None, indexed_size: int = 0) -> int | None:
        """Return the smaller native/quota free space, or None when unlimited.

        Args:
            quota: optional configured byte limit.
            indexed_size: indexed bytes in non-deleted copies in this storage location.
        """
        native = backends.free_space(self.fs, self.root)
        if quota is None:
            return native
        remaining = max(0, quota - indexed_size)
        return remaining if native is None else min(native, remaining)

    def open_read(self, path: str) -> BinaryIO:
        """Open ``path`` for binary reading.

        Raises:
            SymlinkError: ``path`` is, or lies below, a symbolic link (STO-15).
        """
        self.refuse_symlink(path)
        return self.fs.open(self.full_path(path), "rb")  # type: ignore[return-value]

    def md5(self, path: str) -> str:
        """Return the hex md5 of ``path`` by hashing its content (never a stored md5).

        Only a backend that verified hp's own write with the provider's hash
        (:func:`~heliopuddle.storage.backends.verified_md5`) skips the read.
        """
        known = backends.verified_md5(self.fs, self.full_path(path))
        if known is not None:
            return known
        with self.open_read(path) as f:
            return md5_stream(f)

    def root_exists(self) -> bool:
        """Return True if the storage location's root exists.

        On object stores only the bucket or container has to exist (prefixes
        are implicit); elsewhere the root directory itself.
        """
        return bool(self.fs.exists(backends.root_anchor(self.fs.protocol, self.root)))

    @property
    def may_create_root(self) -> bool:
        """True if a missing root is created: ``create_root`` True, or auto off object stores."""
        if self.create_root == "auto":
            return not backends.is_object_store(self.fs.protocol)
        return bool(self.create_root)

    def ensure_root(self) -> None:
        """Make sure the root exists, creating it if ``create_root`` allows (STO-08).

        The root is created with the generic ``fs.mkdir(anchor, create_parents=True)``
        on its anchor (:func:`heliopuddle.storage.backends.root_anchor`): the
        root directory itself, or on an object store the bucket or container.

        Raises:
            StorageRootMissingError: the root is missing and may not be created,
                or creating it failed.
        """
        if self.root_exists():
            return
        if not self.may_create_root:
            raise StorageRootMissingError(self.url)
        anchor = backends.root_anchor(self.fs.protocol, self.root)
        try:
            with contextlib.suppress(FileExistsError):
                self.fs.mkdir(anchor, create_parents=True)
        except Exception as e:
            raise StorageRootMissingError(
                self.url, f"creating it failed: {type(e).__name__}: {e}"
            ) from e
        with contextlib.suppress(Exception):
            self.fs.invalidate_cache()
        if not self.root_exists():
            raise StorageRootMissingError(self.url, "creating it did not take effect")

    def write(
        self,
        path: str,
        source: BinaryIO | str | Path,
        *,
        check: Callable[[], None] | None = None,
    ) -> None:
        """Write ``source`` (a local file path or a binary stream) straight to ``path``.

        The caller names the path: the final path of a new file (under an
        upload reservation) or a staging path (:meth:`write_staged`). The root
        is checked first and created if ``create_root`` allows
        (:meth:`ensure_root`); missing parent directories below it are created.

        A failure part-way (the source raises, or flushing or committing
        fails) never publishes partial bytes where the backend defers its
        commit (the buffered object-store writers): the write is discarded
        (:func:`heliopuddle.storage.backends.abort_write`), so an existing
        object at ``path`` is left as it was. On other backends the file is
        written in place, and a partial file this call created is removed.

        Args:
            path: relative destination path.
            source: a local path or readable binary stream.
            check: optional ownership guard, called before commit and before
                removing a partial object. A refused guard never removes a
                newer owner's object.

        Raises:
            StorageRootMissingError: the root does not exist and may not be created.
            SymlinkError: ``path`` is, or lies below, a symbolic link (STO-15).
        """
        target = self.full_path(path)
        self.refuse_symlink(path)
        # Checked on every write, before makedirs, which could otherwise create it.
        self.ensure_root()
        try:
            with contextlib.suppress(FileExistsError):
                self.fs.makedirs(posixpath.dirname(target), exist_ok=True)
            existed = self._exists_or_unknown(target)
            if isinstance(source, str | Path):
                with open(source, "rb") as src:
                    self._write_to(target, src, existed, check)
            else:
                self._write_to(target, source, existed, check)
        except BaseException as exc:
            if isinstance(exc, Exception) and not self._root_still_exists():
                raise StorageRootMissingError(self.url) from exc
            raise

    def _exists_or_unknown(self, target: str) -> bool:
        """``fs.exists(target)``; True when it cannot be told, so nothing is removed later."""
        try:
            return bool(self.fs.exists(target))
        except Exception:
            return True

    def _write_to(
        self,
        target: str,
        source: BinaryIO,
        existed: bool,
        check: Callable[[], None] | None = None,
    ) -> None:
        """Copy ``source`` to ``target``; on any failure abort and clean up, then re-raise."""
        out = backends.begin_write(self.fs, target)
        deferred = backends.is_deferred(out)
        try:
            shutil.copyfileobj(source, cast(BinaryIO, out), CHUNK_SIZE)
            if check is not None:
                check()
            backends.finish_write(out)
        except BaseException:
            backends.abort_write(out)
            if not existed and not deferred:
                with contextlib.suppress(Exception):
                    if check is not None:
                        check()
                    self.fs.rm(target)
            raise

    def write_staged(self, path: str, token: str, source: BinaryIO | str | Path) -> str:
        """Write ``source`` to the unique staging path of ``path`` for ``token`` (STO-11).

        The staging path is :func:`staging_path` (``<path>.<token>.hp-tmp``),
        so concurrent attempts never share a file. On failure only this
        attempt's staging object is removed; no other ``*.hp-tmp`` name is
        touched, and the final ``path`` is never touched at all.

        Args:
            path: the final relative path.
            token: the operation's token (unique per attempt).
            source: a local file path or a binary stream.

        Returns:
            The relative staging path written.

        Raises:
            StorageRootMissingError: the root does not exist and may not be created.
        """
        staged = staging_path(path, token)
        try:
            self.write(staged, source)
        except BaseException:
            with contextlib.suppress(Exception):
                if self.exists(staged):
                    self.rm(staged)
            raise
        return staged

    @property
    def move_replaces(self) -> bool:
        """Whether :meth:`move` replaces an existing destination on this backend.

        See :func:`heliopuddle.storage.backends.move_replaces`.
        """
        return backends.move_replaces(self.fs.protocol)

    def move(self, src: str, dst: str) -> None:
        """Move ``src`` to ``dst`` (generic ``fs.mv``) within this storage location.

        Whether an existing ``dst`` is replaced depends on the backend
        (:attr:`move_replaces`); where it is not, the move fails and ``dst``
        keeps its content.
        """
        self.refuse_symlink(src)
        self.refuse_symlink(dst)
        self.fs.mv(self.full_path(src), self.full_path(dst))

    def _root_still_exists(self) -> bool:
        try:
            return self.root_exists()
        except Exception:
            return True  # cannot tell: report the original error

    def list_files(self, prefix: str = "") -> Iterator[ListEntry]:
        """Yield every file under ``prefix`` (recursively), with size, mtime and native md5.

        ``prefix`` is a relative directory *project* path (``""`` for the whole
        storage location); with a ``path_prefix``, listed paths are project
        paths too, and a ``prefix`` outside it yields nothing. A prefix that
        does not exist yields nothing. Directories and directory marker
        objects are not yielded.

        The listing streams directory by directory, like ``fs.walk`` with
        ``detail=True`` (one ``fs.ls(detail=True)`` per directory): only the
        directories still to visit are held in memory and the whole tree is
        never built or sorted (SVC-17). Each directory's files, then its
        subdirectories, are visited in name order, so the order is
        deterministic. A directory that vanishes during the listing is
        skipped; any other listing error is raised, so an incomplete listing
        is never mistaken for removed files (``fs.walk`` itself silently
        omits subdirectories it fails to list, so it is not used). Symbolic
        links (to files or directories) are skipped (STO-15).
        """
        scope = listing_scope(self.path_prefix, prefix.rstrip("/"))
        if scope is None:
            return  # the prefix is outside this storage location's path_prefix
        base = self.root if not scope else self._root_path(scope)
        root = self.root.rstrip("/") + "/" if self.root != "/" else "/"
        pending = [self._strip(base)]
        while pending:
            path = pending.pop()
            try:
                listing = cast(list[dict[str, Any]], self.fs.ls(path, detail=True))
            except FileNotFoundError:
                continue
            files: list[tuple[str, dict[str, Any]]] = []
            subdirs: list[str] = []
            for info in listing:
                if backends.is_link(self.fs, info):
                    continue  # a link may point outside the root: never followed (STO-15)
                name = self._strip(str(info.get("name", "")))
                if info.get("type") == "directory":
                    if name and name != path:
                        subdirs.append(name)
                else:
                    files.append((name, info))
            for _name, info in sorted(files, key=lambda f: f[0]):
                entry = self._list_entry(root, info)
                if entry is not None:
                    yield entry
            pending.extend(sorted(subdirs, reverse=True))  # popped in name order

    def _strip(self, name: str) -> str:
        stripped = str(self.fs._strip_protocol(name))  # pyright: ignore[reportPrivateUsage]
        return stripped.rstrip("/") or stripped

    def _list_entry(self, root: str, info: dict[str, Any]) -> ListEntry | None:
        name = str(info.get("name", ""))
        if info.get("type") == "directory" or not name or name.endswith("/"):
            return None
        stripped = str(self.fs._strip_protocol(name))  # pyright: ignore[reportPrivateUsage]
        rel = stripped[len(root) :] if stripped.startswith(root) else stripped
        if not rel:
            return None
        return ListEntry(
            path=f"{self.path_prefix}/{rel}" if self.path_prefix else rel,
            size=int(info.get("size") or 0),
            mtime=backends.info_mtime(info),
            md5=backends.native_md5(info),
        )

    def native_md5(self, path: str) -> str | None:
        """Return the backend's own md5 for ``path`` (hex), or None if it has none.

        Only an indexing shortcut: see :func:`heliopuddle.storage.backends.native_md5`.
        """
        return backends.native_md5(self.fs.info(self.full_path(path)))

    def rm(self, path: str) -> None:
        """Delete ``path``."""
        self.fs.rm(self.full_path(path))

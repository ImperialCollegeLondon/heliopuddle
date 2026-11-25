"""``rclone://<remote>/<path>``: an fsspec filesystem over a local rcd (STO-05).

The remote name is the first path segment (``rclone://box-archive/a b/c.txt``
is ``box-archive:a b/c.txt``). ``storage_options`` give exactly one config
source (``config_block`` or ``config_file``) and optionally ``verify``
(``auto``, ``native``, ``content`` or ``size``). Every call runs in an rclone
session for that config (:mod:`heliopuddle.storage.rclone.session`); only
remotes defined in the config can be addressed.
"""

from __future__ import annotations

import contextlib
import errno
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fsspec import AbstractFileSystem

from heliopuddle.storage.rclone import verify as verifying
from heliopuddle.storage.rclone.config import source_from_options
from heliopuddle.storage.rclone.errors import RcloneConfigError, RcloneError
from heliopuddle.storage.rclone.files import RcloneReadFile, RcloneWriteFile
from heliopuddle.storage.rclone.session import manager

__all__ = ["RcloneFileSystem"]

# rclone skips a copy or move onto an existing object of the same size and
# modification time (within the backend's precision) and, for a move, just
# deletes the source: hp's writes and overwrites must always transfer.
_ALWAYS_TRANSFER = {"_config": {"IgnoreTimes": True}}

_REMOTE_NAME = re.compile(r"^[\w.+@-][\w.+@ -]*$")
_FRACTION = re.compile(r"\.(\d{6})\d+")


def _mtime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(_FRACTION.sub(r".\1", str(value)).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class RcloneFileSystem(AbstractFileSystem):
    """fsspec filesystem for rclone remotes, through a private local rcd.

    Args:
        config_block: name of a stored rclone config block, or
        config_file: path of an ``rclone.conf`` (exactly one of the two).
        verify: how a write is verified: ``auto`` (default), ``native``,
            ``content`` or ``size``.
        **kwargs: passed to ``AbstractFileSystem``.

    Raises:
        RcloneConfigError: not exactly one config source, or an unknown ``verify``.
    """

    protocol = "rclone"
    root_marker = ""

    def __init__(
        self,
        config_block: str | None = None,
        config_file: str | None = None,
        verify: str = "auto",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.source = source_from_options(
            {"config_block": config_block, "config_file": config_file}
        )
        self.verify = verifying.validate_mode(verify)

    @classmethod
    def _strip_protocol(cls, path: Any) -> Any:
        if isinstance(path, list):
            return [cls._strip_protocol(p) for p in path]  # pyright: ignore[reportUnknownVariableType]
        text = str(path)
        if text.startswith("rclone://"):
            text = text[len("rclone://") :]
        elif text.startswith("rclone::"):
            text = text[len("rclone::") :]
        return text.strip("/")

    @staticmethod
    def _get_kwargs_from_urls(path: str) -> dict[str, Any]:
        return {}

    # -- plumbing ---------------------------------------------------------------

    def split(self, path: str) -> tuple[str, str]:
        """Return the rclone fs spec (``remote:``) and the path within it.

        Raises:
            RcloneConfigError: the first segment is not a valid remote name.
        """
        remote, _, rel = self._strip_protocol(path).partition("/")
        if not _REMOTE_NAME.match(remote):
            raise RcloneConfigError(f"not an rclone remote name: {remote!r}")
        return f"{remote}:", rel

    @contextlib.contextmanager
    def _session(self, fs: str) -> Any:
        with manager.use([self.source]) as session:
            if fs[:-1] not in session.remotes:
                raise RcloneConfigError(
                    f"rclone remote {fs[:-1]!r} is not defined in {self.source.describe()}"
                )
            yield session

    def _call(self, endpoint: str, fs: str, **params: Any) -> dict[str, Any]:
        with self._session(fs) as session:
            return session.client.call(endpoint, fs=fs, **params)

    def _key(self, path: str) -> tuple[str, str, str]:
        fs, rel = self.split(path)
        return (self.source.lock_name, fs, rel)

    def _entry(self, fs: str, item: dict[str, Any]) -> dict[str, Any]:
        directory = bool(item.get("IsDir"))
        name = f"{fs[:-1]}/{item.get('Path', '')}".rstrip("/")
        return {
            "name": name,
            "size": 0 if directory else int(item.get("Size", 0)),
            "type": "directory" if directory else "file",
            "mtime": _mtime(item.get("ModTime")),
        }

    # -- reading ----------------------------------------------------------------

    def info(self, path: str, **kwargs: Any) -> dict[str, Any]:
        fs, rel = self.split(path)
        item = self._call("operations/stat", fs, remote=rel, opt={}).get("item")
        if not item:
            raise FileNotFoundError(errno.ENOENT, "no such rclone object", path)
        return self._entry(fs, item)

    def exists(self, path: str, **kwargs: Any) -> bool:
        try:
            self.info(path)
        except FileNotFoundError:
            return False
        return True

    def ls(self, path: str, detail: bool = True, **kwargs: Any) -> list[Any]:
        fs, rel = self.split(path)
        try:
            items = self._call("operations/list", fs, remote=rel, opt={}).get("list") or []
        except (FileNotFoundError, RcloneError):
            info = self.info(path)  # a file lists as itself; otherwise the error stands
            if info["type"] != "file":
                raise
            return [info] if detail else [info["name"]]
        entries = [self._entry(fs, item) for item in items]
        return entries if detail else [e["name"] for e in entries]

    def read_range(self, path: str, start: int, end: int) -> bytes:
        """Bytes ``[start, end)`` of ``path``, streamed from ``--rc-serve``."""
        fs, rel = self.split(path)
        with self._session(fs) as session:
            return session.client.read_range(fs, rel, start, end)

    def modified(self, path: str) -> datetime:
        mtime = self.info(path)["mtime"]
        if mtime is None:
            raise NotImplementedError("the remote reports no modification time")
        return mtime

    def _open(
        self,
        path: str,
        mode: str = "rb",
        block_size: int | None = None,
        autocommit: bool = True,
        cache_options: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        path = self._strip_protocol(path)
        if mode == "rb":
            info = self.info(path)
            if info["type"] != "file":
                raise IsADirectoryError(errno.EISDIR, "is a directory", path)
            return RcloneReadFile(
                self,
                path,
                mode,
                block_size or "default",  # pyright: ignore[reportArgumentType]
                size=info["size"],
                cache_options=cache_options,
            )
        if mode == "wb":
            self.split(path)  # fail fast on a bad remote name
            return RcloneWriteFile(self, path, mode, block_size=block_size, autocommit=autocommit)
        raise NotImplementedError(f"rclone files support modes rb and wb, not {mode!r}")

    # -- writing ----------------------------------------------------------------

    def commit_spool(self, path: str, spool: Path, md5: str, size: int) -> None:
        """Copy a finished spool to ``path`` and verify it (see :class:`RcloneWriteFile`).

        A destination this call created is removed again if the copy or the
        verification fails.
        """
        fs, rel = self.split(path)
        with self._session(fs) as session:
            client = session.client
            existed = client.call("operations/stat", fs=fs, remote=rel, opt={}).get("item")
            try:
                client.call(
                    "operations/copyfile",
                    srcFs=str(spool.parent),
                    srcRemote=spool.name,
                    dstFs=fs,
                    dstRemote=rel,
                    **_ALWAYS_TRANSFER,
                )
                written = verifying.check_written(client, fs, rel, spool, md5, size, self.verify)
            except BaseException:
                verifying.forget(self._key(path))
                if not existed:
                    with contextlib.suppress(Exception):
                        client.call("operations/deletefile", fs=fs, remote=rel)
                raise
        verifying.remember(self._key(path), written)

    def rm_file(self, path: str) -> None:
        fs, rel = self.split(path)
        self._call("operations/deletefile", fs, remote=rel)
        verifying.forget(self._key(path))

    def _rm(self, path: str) -> None:
        self.rm_file(path)

    def rm(self, path: Any, recursive: bool = False, maxdepth: int | None = None) -> None:
        for one in path if isinstance(path, list) else [path]:
            if self.info(one)["type"] == "file":
                self.rm_file(one)
                continue
            fs, rel = self.split(one)
            self._call("operations/purge" if recursive else "operations/rmdir", fs, remote=rel)

    def mv(
        self,
        path1: str,
        path2: str,
        recursive: bool = False,
        maxdepth: int | None = None,
        **kwargs: Any,
    ) -> None:
        """Move a file; an existing destination is replaced (``operations/movefile``)."""
        self._transfer("operations/movefile", path1, path2)
        verifying.move_record(self._key(path1), self._key(path2))

    def cp_file(self, path1: str, path2: str, **kwargs: Any) -> None:
        self._transfer("operations/copyfile", path1, path2)
        verifying.forget(self._key(path2))

    def _transfer(self, endpoint: str, path1: str, path2: str) -> None:
        src, src_rel = self.split(path1)
        dst, dst_rel = self.split(path2)
        with self._session(src), self._session(dst) as session:
            session.client.call(
                endpoint,
                srcFs=src,
                srcRemote=src_rel,
                dstFs=dst,
                dstRemote=dst_rel,
                **_ALWAYS_TRANSFER,
            )

    def mkdir(self, path: str, create_parents: bool = True, **kwargs: Any) -> None:
        fs, rel = self.split(path)
        self._call("operations/mkdir", fs, remote=rel)

    def makedirs(self, path: str, exist_ok: bool = False) -> None:
        if not exist_ok and self.exists(path):
            raise FileExistsError(errno.EEXIST, "already exists", path)
        self.mkdir(path)

    def rmdir(self, path: str) -> None:
        fs, rel = self.split(path)
        self._call("operations/rmdir", fs, remote=rel)

    # -- hp hooks (see heliopuddle.storage.backends) ----------------------------

    def hp_free_space(self, path: str) -> int | None:
        """Free bytes the remote reports (``operations/about``), or None if it reports none."""
        fs, _rel = self.split(path)
        try:
            about = self._call("operations/about", fs)
        except RcloneError as e:
            if "support about" in str(e):
                return None
            raise
        free = about.get("free")
        return int(free) if free is not None else None

    def hp_verified_md5(self, path: str) -> str | None:
        """The md5 of an object hp wrote here, if the provider still vouches for it (else None)."""
        key = self._key(path)
        with self._session(key[1]) as session:
            return verifying.verified_md5(session.client, key)

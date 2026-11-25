"""File objects of :class:`~heliopuddle.storage.rclone.fs.RcloneFileSystem` (STO-05).

Reads fetch byte ranges from rcd's ``--rc-serve``. Writes are buffered
writers (``AbstractBufferedFile``) that spool to a private local file while
computing its md5; nothing reaches the remote until the commit
(``operations/copyfile`` from the spool, then verification). So hp's
``backends.begin_write`` defers their commit like an object store's, and an
aborted write (``discard``) only deletes the spool: a failed source never
reaches the remote, and ``close()`` after an abort cannot commit.

Neither holds a session between calls: each range read and the commit run in
their own :meth:`SessionManager.use`, so streaming a large source does not
hold the config lock, and a copy between two rclone configs in one thread
does not deadlock.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fsspec.spec import AbstractBufferedFile

from heliopuddle.storage.rclone.session import manager

if TYPE_CHECKING:
    from heliopuddle.storage.rclone.fs import RcloneFileSystem

__all__ = ["RcloneReadFile", "RcloneWriteFile"]

BLOCK_SIZE = 8 * 2**20


class RcloneReadFile(AbstractBufferedFile):
    """A seekable reader fetching ranges from ``--rc-serve``."""

    DEFAULT_BLOCK_SIZE = BLOCK_SIZE

    def _fetch_range(self, start: int, end: int) -> bytes:
        fs: Any = self.fs
        return fs.read_range(self.path, start, end)


class RcloneWriteFile(AbstractBufferedFile):
    """A writer spooling to a private local file; :meth:`commit` uploads and verifies it."""

    DEFAULT_BLOCK_SIZE = BLOCK_SIZE

    def __init__(self, fs: RcloneFileSystem, path: str, mode: str = "wb", **kwargs: Any) -> None:
        super().__init__(fs, path, mode, **kwargs)
        self._dir = Path(tempfile.mkdtemp(prefix="hp-rclone-spool-", dir=manager.tmp_dir))
        os.chmod(self._dir, 0o700)
        self.spool = self._dir / "spool"
        self._out = self.spool.open("xb")
        self._md5 = hashlib.md5()
        self._written = 0
        self._finished = False

    def _initiate_upload(self) -> None:
        """Nothing to initiate: the spool is opened by the constructor."""

    def _upload_chunk(self, final: bool = False) -> bool:  # pyright: ignore[reportIncompatibleMethodOverride]
        with self.buffer.getbuffer() as data:
            self._out.write(data)
            self._md5.update(data)
            self._written += len(data)
        if final:
            self._out.close()
            if self.autocommit:
                self.commit()
        return True

    def commit(self) -> None:
        """Copy the spool to the destination and verify it, then delete the spool.

        Raises:
            OSError: the copy or the verification failed; a destination this
                commit created is removed.
        """
        if self._finished:
            return
        try:
            fs: Any = self.fs
            fs.commit_spool(self.path, self.spool, self._md5.hexdigest(), self._written)
        finally:
            self._cleanup()

    def discard(self) -> None:
        """Drop the spool; nothing reaches the remote."""
        self._cleanup()

    def _cleanup(self) -> None:
        self._finished = True
        with contextlib.suppress(Exception):
            self._out.close()
        shutil.rmtree(self._dir, ignore_errors=True)

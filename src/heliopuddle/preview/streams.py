"""Stream wrappers that keep previews bounded and tell storage failures from bad content."""

from __future__ import annotations

import io
from typing import BinaryIO


class StorageReadError(OSError):
    """The storage behind a preview could not be read (as opposed to bad content).

    The message names only the exception type, never a path or credential.
    """


class GuardedReader(io.RawIOBase):
    """A seekable reader over a storage stream that records the first storage failure.

    Any exception raised by the source (except ``FileNotFoundError``, which
    means the object is gone) is re-raised as :class:`StorageReadError` and kept
    in :attr:`failure`, so a parser that wraps it in its own error type can
    still be recognised as a storage failure.

    Args:
        source: the open storage stream.
    """

    def __init__(self, source: BinaryIO) -> None:
        super().__init__()
        self.source = source
        self.failure: StorageReadError | None = None
        self.bytes_read = 0

    def _fail(self, error: Exception) -> StorageReadError:
        if isinstance(error, FileNotFoundError):
            raise error
        self.failure = StorageReadError(f"storage read failed ({type(error).__name__})")
        return self.failure

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        try:
            return bool(self.source.seekable())
        except Exception as error:
            raise self._fail(error) from None

    def read(self, size: int | None = -1) -> bytes:
        try:
            data = self.source.read(-1 if size is None else size)
        except Exception as error:
            raise self._fail(error) from None
        self.bytes_read += len(data)
        return data

    def readinto(self, buffer: memoryview | bytearray) -> int:  # type: ignore[override]
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        try:
            return self.source.seek(offset, whence)
        except Exception as error:
            raise self._fail(error) from None

    def tell(self) -> int:
        try:
            return self.source.tell()
        except Exception as error:
            raise self._fail(error) from None

    def close(self) -> None:
        try:
            self.source.close()
        finally:
            super().close()


class LimitedReader(io.RawIOBase):
    """A reader that returns at most ``limit`` bytes of its source, then end of file.

    Args:
        source: the stream to read.
        limit: the most bytes ever read from ``source``.
        size: the source's total size if known, to tell whether the limit cut it.
    """

    def __init__(self, source: BinaryIO | io.RawIOBase, limit: int, size: int | None) -> None:
        super().__init__()
        self.source = source
        self.limit = limit
        self.size = size
        self.consumed = 0

    @property
    def cut(self) -> bool:
        """True when the limit was reached and the source has (or may have) more."""
        if self.consumed < self.limit:
            return False
        return self.size is None or self.size > self.limit

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: memoryview | bytearray) -> int:  # type: ignore[override]
        want = min(len(buffer), self.limit - self.consumed)
        if want <= 0:
            return 0
        data = self.source.read(want) or b""
        buffer[: len(data)] = data
        self.consumed += len(data)
        return len(data)


def limited_text(
    source: BinaryIO | io.RawIOBase, limit: int, size: int | None
) -> tuple[io.TextIOWrapper, LimitedReader]:
    """Open the first ``limit`` bytes of ``source`` as UTF-8 text (bad bytes replaced).

    Args:
        source: the binary stream.
        limit: the most bytes read.
        size: the source's total size if known.

    Returns:
        The text stream (universal newlines off, as ``csv`` wants) and the
        limiter, whose :attr:`LimitedReader.cut` tells whether text was left out.
    """
    limiter = LimitedReader(source, limit, size)
    text = io.TextIOWrapper(
        io.BufferedReader(limiter), encoding="utf-8-sig", errors="replace", newline=""
    )
    return text, limiter

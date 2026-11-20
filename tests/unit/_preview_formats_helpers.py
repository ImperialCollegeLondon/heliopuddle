"""Shared generators and helpers for the preview format tests."""

from __future__ import annotations

import io
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from heliopuddle.preview import build_preview
from heliopuddle.server.schemas import FilePreview

GB = 1_000_000_000


BIG = 200_000_000  # bytes in the generated "200 MB" files; never materialised


class Generated(io.RawIOBase):
    """A seekable stream of ``head`` then ``unit`` repeated up to ``size`` bytes, made on demand.

    Counts the bytes handed out, so tests can prove how much a preview read.
    """

    def __init__(self, head: bytes, unit: bytes, size: int) -> None:
        super().__init__()
        self.head, self.unit, self.size = head, unit, size
        self.pos = 0
        self.bytes_read = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self.pos, io.SEEK_END: self.size}[whence]
        self.pos = base + offset
        return self.pos

    def tell(self) -> int:
        return self.pos

    def _bytes(self, start: int, count: int) -> bytes:
        out = b""
        if start < len(self.head):
            out = self.head[start : start + count]
            start, count = len(self.head), count - len(out)
        if count > 0:
            offset = (start - len(self.head)) % len(self.unit)
            repeat = self.unit * ((offset + count) // len(self.unit) + 1)
            out += repeat[offset : offset + count]
        return out

    def readinto(self, buffer: Any) -> int:
        count = min(len(buffer), self.size - self.pos)
        if count <= 0:
            return 0
        data = self._bytes(self.pos, count)
        buffer[: len(data)] = data
        self.pos += len(data)
        self.bytes_read += len(data)
        return len(data)


def preview(name: str, data: bytes | io.RawIOBase, size: int | None = None) -> FilePreview:
    stream = io.BytesIO(data) if isinstance(data, bytes) else data
    if size is None and isinstance(data, bytes):
        size = len(data)
    return build_preview(
        file_id=7,
        name=name,
        opener=lambda: stream,  # type: ignore[arg-type, return-value]
        size=size,
        max_bytes=GB,
        storage_location="primary",
    )


def rows(p: FilePreview, index: int = 0) -> list[list[Any]]:
    return p.tables[index].rows


def names(p: FilePreview, index: int = 0) -> list[str]:
    return [c.name for c in p.tables[index].columns]


def _parquet(table: pa.Table, **kwargs: Any) -> bytes:
    sink = io.BytesIO()
    pq.write_table(table, sink, **kwargs)
    return sink.getvalue()

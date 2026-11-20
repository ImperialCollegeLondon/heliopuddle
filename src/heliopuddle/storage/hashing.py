"""Streamed content hashing."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

CHUNK_SIZE = 8 * 1024 * 1024


@dataclass
class HashReads:
    """Content bytes actually consumed by hash readers in one operation."""

    bytes: int = 0


_reads: ContextVar[HashReads | None] = ContextVar("hash_reads", default=None)


@contextmanager
def count_hash_reads() -> Iterator[HashReads]:
    """Measure hash reads, including nested repair/verify operations, without counting metadata."""
    counter = HashReads()
    token = _reads.set(counter)
    try:
        yield counter
    finally:
        _reads.reset(token)


def md5_stream(stream: BinaryIO, chunk_size: int = CHUNK_SIZE) -> str:
    """Return the hex md5 of everything left in ``stream``, read in chunks."""
    digest = hashlib.md5(usedforsecurity=False)
    counter = _reads.get()
    while chunk := stream.read(chunk_size):
        if counter is not None:
            counter.bytes += len(chunk)
        digest.update(chunk)
    return digest.hexdigest()


def md5_file(path: str | Path, chunk_size: int = CHUNK_SIZE) -> str:
    """Return the hex md5 of a local file."""
    with open(path, "rb") as f:
        return md5_stream(f, chunk_size)

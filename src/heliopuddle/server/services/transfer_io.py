"""Store I/O of a verified transfer: byte copy and same-object refusal (SVC-19/20)."""

from __future__ import annotations

import contextlib
import posixpath
from collections.abc import Callable
from typing import BinaryIO, cast

from heliopuddle.server.models import transfers
from heliopuddle.storage import StorageLocation, backends
from heliopuddle.storage.location import CheckedReader


def _copy_bytes(
    source: StorageLocation,
    destination: StorageLocation,
    path: str,
    write_path: str,
    check: Callable[[], None],
) -> None:
    source_path = source.full_path(path)
    target = destination.full_path(write_path)
    source.refuse_symlink(path)
    destination.refuse_symlink(write_path)
    check()
    # Generic copy expands globs on some filesystems; such paths must be streamed literally.
    if backends.same_filesystem(source.fs, destination.fs) and not any(
        char in source_path or char in target for char in "*?["
    ):
        destination.ensure_root()
        with contextlib.suppress(FileExistsError):
            destination.fs.makedirs(posixpath.dirname(target), exist_ok=True)
        source.fs.copy(source_path, target)
        destination.fs.invalidate_cache(posixpath.dirname(target))
    else:
        with source.open_read(path) as stream:
            destination.write(write_path, cast(BinaryIO, CheckedReader(stream, check)), check=check)
    check()


def _refuse_same_object(
    plan: transfers.TransferSnapshot, source_store: StorageLocation, destination: StorageLocation
) -> None:
    path = plan.source.path
    if source_store.fs == destination.fs and source_store.full_path(path) == destination.full_path(
        path
    ):
        raise ValueError("source and destination refer to the same storage object")

"""File previews (API-18): pick the readable copy, build its preview, cache it.

The copy is chosen as for ``GET /files/{id}/content``: the first configured
copy in ``available`` state, else ``overwriting`` (which still holds the
original). Previews are cached in an in-process LRU keyed on the file, the
storage location and the copy's size and modification time, so a changed copy
is read again. See docs/design/preview.md.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Hashable

from prefect import task

from heliopuddle.preview import StorageReadError, build_preview
from heliopuddle.server.context import get_context
from heliopuddle.server.schemas import FilePreview
from heliopuddle.server.services.content import readable_copies
from heliopuddle.server.services.stores import store_for
from heliopuddle.storage import backends
from heliopuddle.storage.credentials import StorageLocationIncomplete

CACHE_SIZE = 64
"""Previews kept in memory per server process."""


class _Cache:
    """A thread-safe LRU of previews."""

    def __init__(self, size: int) -> None:
        self.size = size
        self._items: OrderedDict[Hashable, FilePreview] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: Hashable) -> FilePreview | None:
        with self._lock:
            item = self._items.get(key)
            if item is not None:
                self._items.move_to_end(key)
            return item

    def put(self, key: Hashable, preview: FilePreview) -> None:
        with self._lock:
            self._items[key] = preview
            self._items.move_to_end(key)
            while len(self._items) > self.size:
                self._items.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


cache = _Cache(CACHE_SIZE)


@task(name="preview_file")
def preview_file(file_id: int) -> FilePreview:
    """Build (or return the cached) preview of a live file's first readable copy.

    Args:
        file_id: the authorized file id.

    Returns:
        The preview; content that cannot be parsed gives kind ``error``.

    Raises:
        FileNotFoundError: the file is deleted, or no readable copy exists.
        OSError: the backing storage could not be read (the message names no
            path or credential).
    """
    ctx = get_context()
    file, copies = readable_copies(ctx, file_id, "file not found")
    incomplete: StorageLocationIncomplete | None = None
    for copy in copies:
        if ctx.storage_location_config(file.project, copy.storage_location) is None:
            continue
        try:
            store = store_for(ctx, file.project, copy.storage_location)
            info = store.info(file.path)
            key = (
                ctx.settings.resolved_database_url,
                file.id,
                copy.storage_location,
                info.size,
                info.mtime,
            )
            cached = cache.get(key)
            if cached is not None:
                return cached
            store.refuse_symlink(file.path)  # the local path below is opened directly
            full_path = store.full_path(file.path)
            preview = build_preview(
                file_id=file.id,
                name=file.path,
                opener=lambda store=store: store.open_read(file.path),
                size=info.size,
                max_bytes=int(ctx.settings.preview_max_bytes),
                local_path=backends.local_path(store.fs, full_path),
                storage_location=copy.storage_location,
            )
        except FileNotFoundError:
            continue
        except StorageLocationIncomplete as error:
            incomplete = error  # another copy may be readable
            continue
        except StorageReadError as error:
            raise OSError(str(error)) from None
        except Exception as error:
            raise OSError(f"backing storage read failed ({type(error).__name__})") from None
        cache.put(key, preview)
        return preview
    if incomplete is not None:
        raise incomplete
    raise FileNotFoundError("file content not found")

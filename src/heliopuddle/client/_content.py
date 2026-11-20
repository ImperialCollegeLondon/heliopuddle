"""Reading file content: preview, open and download."""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
from pathlib import Path
from typing import Any, BinaryIO

import httpx

from heliopuddle.client._files import FilesMixin
from heliopuddle.client.errors import IntegrityError
from heliopuddle.server.schemas import FilePreview, FileRead, FileState
from heliopuddle.storage.hashing import CHUNK_SIZE
from heliopuddle.storage.location import TEMP_SUFFIX, StorageLocation


class ContentMixin(FilesMixin):
    """``preview``, ``open`` and ``download`` of :class:`HelioPuddleClient`."""

    def preview(self, path_or_id: str | int) -> FilePreview:
        """Return a bounded preview of a file's content (``GET /files/{id}/preview``).

        The server reads the first readable copy, so this needs no storage
        credentials. Content that cannot be parsed comes back with kind ``error``.

        Args:
            path_or_id: the file id, or the path of a live file.

        Returns:
            The preview: tables, text or attributes, depending on the format.

        Raises:
            NotFoundError: unknown or deleted file, no live file at the path, an
                id from another project, or no readable copy.
            HelioPuddleError: ``503`` if the server could not read the storage.
        """
        file_id = self._resolve(path_or_id).id  # an id must be in this project (CLI-08)
        response = self._request("GET", f"/files/{file_id}/preview")
        return FilePreview.model_validate(response.json())

    def open(self, path_or_id: str | int) -> BinaryIO:
        """Open a file's content for binary reading, directly or through the API.

        Uses the first ``available`` copy (in storage-location order) with that
        storage location's URL and options from the API. The content is not
        checked against the hash; :meth:`download` does that.

        Args:
            path_or_id: the file id, or the path of a live file.

        Returns:
            A binary file object; close it (or use it as a context manager).

        Raises:
            NotFoundError: unknown file, no live file at the path, or an id
                from another project (the store is not touched).
            FileNotFoundError: the file has no ``available`` copy.
        """
        return self._open_copy(self._resolve(path_or_id))

    def download(self, path_or_id: str | int, dest: str | Path) -> Path:
        """Stream a file from the store to ``dest`` and check its md5.

        The content goes to a temporary file next to ``dest`` and is moved into
        place only if its md5 matches the record. Missing parent directories
        of ``dest`` are created.

        Args:
            path_or_id: the file id, or the path of a live file.
            dest: target file, or an existing directory (the file's name is used).

        Returns:
            The path written.

        Raises:
            NotFoundError: unknown file, no live file at the path, or an id
                from another project (the store is not touched).
            FileNotFoundError: the file has no ``available`` copy.
            IntegrityError: the content does not match the record's hash; the
                partial download is removed and ``dest`` is left untouched.
        """
        file = self._resolve(path_or_id)
        target = Path(dest)
        if target.is_dir():
            target = target / file.name
        with self._open_copy(file) as src:
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                dir=target.parent, prefix=f".{target.name}.", suffix=TEMP_SUFFIX
            )
            tmp = Path(tmp_name)
            try:
                digest = hashlib.md5(usedforsecurity=False)
                with os.fdopen(fd, "wb") as out:
                    while chunk := src.read(CHUNK_SIZE):
                        digest.update(chunk)
                        out.write(chunk)
                actual = digest.hexdigest()
                if actual != file.hash.lower():
                    raise IntegrityError(file, actual)
                os.replace(tmp, target)
            finally:
                tmp.unlink(missing_ok=True)
        return target

    def _open_copy(self, file: FileRead) -> BinaryIO:
        """Open a copy of ``file`` that serves its recorded content.

        An ``available`` copy is preferred; an ``overwriting`` copy still holds
        the recorded (original) content at the final path until the overwrite
        is published, so it is read too.
        """
        readable = (FileState.AVAILABLE, FileState.OVERWRITING)
        copy = min(
            (c for c in file.storage_locations if c.state in readable),
            key=lambda c: readable.index(c.state),
            default=None,
        )
        if copy is None:
            states = ", ".join(f"{c.storage_location}={c.state}" for c in file.storage_locations)
            raise FileNotFoundError(
                f"file {file.id} ({file.path!r}) has no available copy ({states or 'no copies'})"
            )
        location = self._transfer_location(copy.storage_location)
        if location is None:
            return io.BufferedReader(
                _ResponseStream(self._request("GET", f"/files/{file.id}/content", stream=True))
            )
        return StorageLocation(
            location.url, location.storage_options, location.create_root
        ).open_read(file.path)


class _ResponseStream(io.RawIOBase):
    """A bounded-memory binary reader that owns its streaming HTTP response."""

    def __init__(self, response: httpx.Response) -> None:
        self._response = response
        self._chunks = response.iter_bytes(CHUNK_SIZE)
        self._pending = b""

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        chunk = self._pending or next(self._chunks, b"")
        count = min(len(buffer), len(chunk))
        buffer[:count] = chunk[:count]
        self._pending = chunk[count:]
        return count

    def close(self) -> None:
        try:
            self._response.close()
        finally:
            super().close()

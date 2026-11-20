"""Reader-authorized file content, opened without exposing storage credentials."""

from __future__ import annotations

import contextlib
from typing import BinaryIO

from prefect import task

from heliopuddle.server import models
from heliopuddle.server.context import AppContext, get_context
from heliopuddle.server.schemas import FileRead, FileState, FileStorageLocationRead
from heliopuddle.server.services.stores import store_for
from heliopuddle.storage.hashing import CHUNK_SIZE


def readable_copies(
    ctx: AppContext, file_id: int, missing: str
) -> tuple[FileRead, list[FileStorageLocationRead]]:
    """Load a live file and its readable copies in preference order.

    Args:
        ctx: the application context.
        file_id: the authorized file id.
        missing: the message of the ``FileNotFoundError`` for a missing or
            deleted file.

    Returns:
        The file and its ``available`` copies, then ``overwriting`` ones
        (which still hold the original).

    Raises:
        FileNotFoundError: the file does not exist or is deleted.
    """
    with ctx.sessionmaker() as session:
        row = models.get_file(session, file_id)
        if row is None or row.deleted_at is not None:
            raise FileNotFoundError(missing)
        file = models.to_file_read(row)
    readable = (FileState.AVAILABLE, FileState.OVERWRITING)
    copies = sorted(
        (copy for copy in file.storage_locations if copy.state in readable),
        key=lambda copy: readable.index(copy.state),
    )
    return file, copies


@task(name="open_file_content")
def open_file_content(file_id: int) -> tuple[BinaryIO, bytes]:
    """Open the first configured readable copy and read its first chunk.

    Args:
        file_id: the authorized file id.

    Returns:
        The open stream and its first chunk. The caller must close the stream.

    Raises:
        FileNotFoundError: no live, readable object exists.
        OSError: the backing store could not be opened or read.
    """
    ctx = get_context()
    file, copies = readable_copies(ctx, file_id, "file content not found")
    for copy in copies:
        if ctx.storage_location_config(file.project, copy.storage_location) is None:
            continue
        try:
            with contextlib.ExitStack() as opened:
                stream = opened.enter_context(
                    store_for(ctx, file.project, copy.storage_location).open_read(file.path)
                )
                first = stream.read(CHUNK_SIZE)
                opened.pop_all()
                return stream, first
        except FileNotFoundError:
            continue
        except Exception as error:
            raise OSError(f"backing storage read failed ({type(error).__name__})") from None
    raise FileNotFoundError("file content not found")

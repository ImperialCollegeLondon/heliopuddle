"""Write API-uploaded content under an exclusive staging claim."""

from __future__ import annotations

from typing import BinaryIO, cast

from prefect import task

from heliopuddle.server.context import get_context
from heliopuddle.server.models import transitions as tr
from heliopuddle.server.models import uploads
from heliopuddle.server.services.stores import store_for
from heliopuddle.storage.credentials import StorageLocationIncomplete
from heliopuddle.storage.location import CheckedReader, staging_path


class ContentWriteFailed(OSError):
    """The backing store rejected an API-based write; no connection details are exposed."""


@task(name="write_upload_content")
def write_upload_content(token: str, source: BinaryIO) -> None:
    """Write validated API content under an exclusive, renewable staging claim.

    Args:
        token: the client's current upload token.
        source: a seekable binary spool whose hash and size were checked.

    Raises:
        uploads.TokenNotCurrent: the upload was finished or taken over.
        tr.StaleOperation: ownership changed during the write.
        OSError: the backing store could not be written.
    """
    ctx = get_context()
    with ctx.write_session() as session:
        snap = uploads.claim(session, token, ctx.settings.op_lease, staged=True)

    def check() -> None:
        with ctx.write_session() as session:
            copy = tr.lock_copy(session, snap.copy_id, token=snap.token, generation=snap.generation)
            copy.op_deadline = tr.clock() + ctx.settings.op_lease

    try:
        check()
        try:
            store = store_for(ctx, snap.project, snap.storage_location)
            check()
            store.write(
                staging_path(snap.path, token),
                cast(BinaryIO, CheckedReader(source, check)),
                check=check,
            )
        except (tr.StaleOperation, tr.CopyBusy, StorageLocationIncomplete):
            raise
        except Exception as error:
            raise ContentWriteFailed(
                f"backing storage write failed ({type(error).__name__})"
            ) from None
    finally:
        with ctx.write_session() as session:
            uploads.unclaim(session, snap, token, ctx.settings.op_lease)

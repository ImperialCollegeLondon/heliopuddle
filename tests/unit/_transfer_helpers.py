"""Shared helpers and fixtures for the split transfer tests."""

from __future__ import annotations

import io
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from sqlalchemy import select

from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import FileStorageLocation
from heliopuddle.server.models import uploads
from heliopuddle.server.models.snapshots import CopySnapshot, snapshot
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services import transfer
from heliopuddle.server.services.upload import (
    complete_upload,
    object_present,
)
from tests.adversarial import (
    clock,  # noqa: F401 - fixture
    config_dir,  # noqa: F401 - fixture
    protocol,  # noqa: F401 - fixture
    store,
)
from tests.lifecycle import md5

PATH = "p/data_v1.dat"


DATA = b"the verified source content"


OTHER = b"a different destination"


@pytest.fixture(autouse=True)
def limits(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    held: list[str] = []

    @contextmanager
    def acquire(names: str | list[str], **_kwargs: Any) -> Iterator[None]:
        held.extend([names] if isinstance(names, str) else names)
        yield

    monkeypatch.setattr(transfer, "hp_concurrency", acquire)
    monkeypatch.setattr(transfer, "ensure_concurrency_limit", lambda _name: None)
    return held


def upload(
    ctx: AppContext,
    data: bytes = DATA,
    storage_location: str = "primary",
    path: str = PATH,
    *,
    overwrite: bool = False,
) -> int:
    """Create a verified copy through the real reservation and complete protocol."""
    present = object_present(ctx, "demo", storage_location, path)
    with ctx.write_session() as session:
        reservation = uploads.reserve(
            session,
            project="demo",
            storage_location=storage_location,
            path=path,
            hash=md5(data),
            size=len(data),
            lease=ctx.settings.op_lease,
            final_exists=present,
            overwrite=overwrite,
        )
    if reservation.token is not None:
        assert reservation.write_path is not None
        store(ctx, storage_location).write(reservation.write_path, io.BytesIO(data))
        assert complete_upload(reservation.token).state == FileState.AVAILABLE
    return reservation.file_id


def copy(ctx: AppContext, name: str = "primary", path: str = PATH) -> CopySnapshot:
    with ctx.sessionmaker() as session:
        row = next(
            c
            for c in session.scalars(select(FileStorageLocation))
            if c.storage_location.name == name and c.file.path == path
        )
        snap = snapshot(session, row.id)
        assert snap is not None
        return snap

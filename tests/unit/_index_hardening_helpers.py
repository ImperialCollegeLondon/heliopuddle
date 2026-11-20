"""Shared helpers and fixtures for the split index_hardening tests."""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update

from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import (
    File,
    FileStorageLocation,
)

OLD = timedelta(hours=2)


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def put(root: Path, path: str, data: bytes, age: timedelta = OLD) -> Path:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    when = (datetime.now(UTC) - age).timestamp()
    os.utime(target, (when, when))
    return target


def copy_row(ctx: AppContext, path: str, location: str = "primary") -> FileStorageLocation:
    with ctx.sessionmaker() as session:
        row = session.scalar(
            select(FileStorageLocation)
            .join(File, File.id == FileStorageLocation.file_id)
            .where(File.path == path, FileStorageLocation.storage_location.has(name=location))
            .order_by(File.id.desc())
        )
        assert row is not None, path
        session.expunge(row)
        return row


def file_row(ctx: AppContext, path: str) -> File:
    with ctx.sessionmaker() as session:
        row = session.scalar(select(File).where(File.path == path).order_by(File.id.desc()))
        assert row is not None, path
        session.expunge(row)
        return row


def set_copy(ctx: AppContext, path: str, **values: Any) -> None:
    copy_id = copy_row(ctx, path).id
    with ctx.write_session() as session:
        session.execute(
            update(FileStorageLocation).where(FileStorageLocation.id == copy_id).values(**values)
        )


@pytest.fixture
def root(store: Path) -> Path:
    return store

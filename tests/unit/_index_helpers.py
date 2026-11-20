"""Shared helpers and fixtures for the split index tests."""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from heliopuddle.server import models
from heliopuddle.server.context import AppContext
from heliopuddle.server.schemas import FileRead, FileState

OLD = timedelta(hours=2)


@pytest.fixture
def stores(tmp_path: Path, store: Path) -> dict[str, Path]:
    """Root directories by storage location (primary accepts external changes)."""
    return {"primary": store, "secondary": tmp_path / "store2"}


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def put(root: Path, path: str, data: bytes, age: timedelta = OLD) -> Path:
    """Write a file into a store with its mtime ``age`` ago."""
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    when = (datetime.now(UTC) - age).timestamp()
    os.utime(target, (when, when))
    return target


def register(ctx: AppContext, location: str, path: str, data: bytes) -> models.RegisterResult:
    with ctx.write_session() as session:
        return models.register_file(
            session,
            project="demo",
            storage_location=location,
            path=path,
            hash=_md5(data),
            size=len(data),
            rules=ctx.projects["demo"].versioning,
        )


def by_path(ctx: AppContext, path: str, include_deleted: bool = False) -> FileRead:
    with ctx.sessionmaker() as session:
        files, _ = models.list_files(session, "demo", path=path, include_deleted=include_deleted)
        assert files, path
        return models.to_file_read(sorted(files, key=lambda f: f.id)[-1])


def copy_state(rec: FileRead, location: str) -> FileState | None:
    for c in rec.storage_locations:
        if c.storage_location == location:
            return c.state
    return None


def set_index(ctx: AppContext, **values: Any) -> None:
    cfg = ctx.projects["demo"].index
    for key, value in values.items():
        setattr(cfg, key, value)

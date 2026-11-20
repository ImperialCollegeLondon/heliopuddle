"""Invariants over both the store and the DB, shared by the lifecycle tests.

docs/design/operations.md: an ``available`` copy's hash is the store's and the
file's; a live path has at most one live file; temporary ``*.hp-tmp`` files
exist only while an operation owns the path.
"""

from __future__ import annotations

import hashlib
import importlib
import pkgutil
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import func, select

from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import File, FileStorageLocation
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services.stores import store_for
from heliopuddle.storage import StorageLocation
from heliopuddle.storage.location import TEMP_SUFFIX


def md5(data: bytes) -> str:
    """Hex md5 of ``data``."""
    return hashlib.md5(data).hexdigest()


@dataclass(frozen=True)
class CopyRow:
    """A copy as the DB holds it, with its file's columns."""

    copy_id: int
    file_id: int
    path: str
    storage_location: str
    state: FileState
    token: str | None
    copy_hash: str | None
    copy_size: int | None
    file_hash: str
    file_size: int
    deleted: bool


def copies(ctx: AppContext) -> list[CopyRow]:
    """Every copy of project ``demo`` with its file."""
    with ctx.sessionmaker() as session:
        rows = session.scalars(select(FileStorageLocation).order_by(FileStorageLocation.id))
        return [
            CopyRow(
                c.id,
                c.file_id,
                c.file.path,
                c.storage_location.name,
                c.state,
                c.op_token,
                c.hash,
                c.size,
                c.file.hash,
                c.file.size,
                c.file.deleted_at is not None,
            )
            for c in rows
        ]


def store_bytes(store: StorageLocation, path: str) -> bytes | None:
    """The object's content, or None if it does not exist."""
    try:
        with store.open_read(path) as f:
            return f.read()
    except FileNotFoundError:
        return None


def temp_files(store: StorageLocation) -> list[str]:
    """Every ``*.hp-tmp`` object in the store (relative paths)."""
    return sorted(p for p in _all(store) if p.endswith(TEMP_SUFFIX))


def _all(store: StorageLocation) -> list[str]:
    try:
        found = store.fs.find(store.root, withdirs=False)
    except FileNotFoundError:
        return []
    root = str(store.fs._strip_protocol(store.root)).rstrip("/") + "/"  # pyright: ignore[reportPrivateUsage]
    out = []
    for name in found:
        stripped = str(store.fs._strip_protocol(name))  # pyright: ignore[reportPrivateUsage]
        out.append(stripped[len(root) :] if stripped.startswith(root) else stripped)
    return out


def assert_consistent(ctx: AppContext, project: str = "demo", *, allow_temp: bool = False) -> None:
    """Assert the store/DB invariants for every copy of ``project``.

    - an ``available`` copy of a live file: the object exists and its md5 and
      size equal the copy's and the file's;
    - at most one live file per path;
    - unless ``allow_temp``, no ``*.hp-tmp`` file next to a path no operation owns.
    """
    rows = copies(ctx)
    live_paths = [r.path for r in rows if not r.deleted]
    with ctx.sessionmaker() as session:
        dupes = session.execute(
            select(File.path, func.count())
            .where(File.deleted_at.is_(None))
            .group_by(File.path)
            .having(func.count() > 1)
        ).all()
    assert dupes == [], dupes
    owned = {(r.storage_location, r.path) for r in rows if r.token is not None}
    for row in rows:
        if row.deleted or row.state != FileState.AVAILABLE:
            continue
        store = store_for(ctx, project, row.storage_location)
        data = store_bytes(store, row.path)
        assert data is not None, f"{row.path}: available but absent"
        assert md5(data) == row.copy_hash == row.file_hash, row
        assert len(data) == row.copy_size == row.file_size, row
    if allow_temp:
        return
    for name in {r.storage_location for r in rows}:
        store = store_for(ctx, project, name)
        for tmp in temp_files(store):
            assert any(loc == name and tmp.startswith(p + ".") for loc, p in owned), (
                f"orphan {tmp} with no owner; live paths {live_paths}"
            )


def _upload_modules() -> list[Any]:
    package = importlib.import_module("heliopuddle.server.services.upload")
    return [
        importlib.import_module(info.name)
        for info in pkgutil.iter_modules(package.__path__, package.__name__ + ".")
    ]


def upload_attr(name: str) -> Any:
    """Return the upload package's current ``name`` (a helper any of its modules binds)."""
    for module in _upload_modules():
        if hasattr(module, name):
            return getattr(module, name)
    raise AttributeError(name)


def patch_upload(monkeypatch: pytest.MonkeyPatch, name: str, value: Any) -> None:
    """Patch ``name`` in every upload module that binds it, so the patch holds package-wide."""
    for module in _upload_modules():
        if hasattr(module, name):
            monkeypatch.setattr(module, name, value)

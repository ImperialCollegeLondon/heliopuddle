"""Index hardening: streaming listing, set-free removals and scale."""

from __future__ import annotations

import io
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import fsspec
import pytest
from fsspec.implementations.memory import MemoryFileSystem
from sqlalchemy import select

from heliopuddle.server import models
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import (
    File,
    FileStorageLocation,
    utcnow,
)
from heliopuddle.server.models import index as index_db
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services import index_finish
from heliopuddle.server.services.index import (
    index_full,
)
from heliopuddle.storage import StorageLocation
from tests.unit._index_hardening_helpers import (
    OLD,
    _md5,
    copy_row,
    file_row,
    put,
    root,
)

_FIXTURES = (root,)  # referenced so ruff sees the imports used

pytestmark = pytest.mark.usefixtures("inline_prefect")


# --- SVC-17: streaming listing and set-free removals -----------------------------------


class _NoFind(MemoryFileSystem):
    """A memory filesystem whose whole-tree listing is forbidden."""

    protocol = "nofind"
    root_marker = "/"

    def find(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("list_files must stream with walk, not find")


fsspec.register_implementation("nofind", _NoFind, clobber=True)


@pytest.mark.req("SVC-17")
def test_list_files_streams_with_walk_in_a_deterministic_order() -> None:
    _NoFind.store = {}
    _NoFind.pseudo_dirs = [""]
    loc = StorageLocation("nofind://lst")
    for path in ("b/2.dat", "a/z.dat", "a/c/1.dat", "top.dat", "a/a.dat"):
        loc.write(path, io.BytesIO(b"x"))

    listing = loc.list_files()
    first = next(listing)  # yields before the whole tree is read
    rest = [e.path for e in listing]

    assert [first.path, *rest] == ["top.dat", "a/a.dat", "a/z.dat", "a/c/1.dat", "b/2.dat"]
    assert [e.path for e in loc.list_files("a")] == ["a/a.dat", "a/z.dat", "a/c/1.dat"]
    assert list(loc.list_files("missing")) == []


@pytest.mark.req("SVC-17")
def test_listing_errors_other_than_missing_are_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    _NoFind.store = {}
    _NoFind.pseudo_dirs = [""]
    loc = StorageLocation("nofind://err")
    loc.write("a/b.dat", io.BytesIO(b"x"))
    real_ls = _NoFind.ls

    def flaky(self: _NoFind, path: str, detail: bool = True, **kwargs: Any) -> Any:
        if path.rstrip("/").endswith("/a"):
            raise PermissionError(path)
        return real_ls(self, path, detail=detail, **kwargs)

    monkeypatch.setattr(_NoFind, "ls", flaky)
    with pytest.raises(PermissionError):
        list(loc.list_files())


@pytest.mark.req("SVC-17")
def test_removals_are_detected_without_a_set_of_listed_paths(ctx: AppContext, root: Path) -> None:
    for i in range(5):
        put(root, f"s/f{i}.dat", f"{i}".encode())
    index_full.fn("demo", "primary")
    put(root, "s/f0.dat", b"0", age=timedelta(seconds=1))  # settling: seen, not diffed
    (root / "s/f3.dat").unlink()

    summary = index_full.fn("demo", "primary")

    assert (summary.removed, summary.skipped) == (1, 1)
    for i in (0, 1, 2, 4):
        copy = copy_row(ctx, f"s/f{i}.dat")
        assert copy.state == FileState.AVAILABLE
        assert copy.last_seen_at == summary.started_at
    assert file_row(ctx, "s/f3.dat").deleted_at is not None


@pytest.mark.req("SVC-17")
def test_removal_candidates_page_by_id(ctx: AppContext, root: Path) -> None:
    for i in range(7):
        put(root, f"k/{i}.dat", b"k")
    index_full.fn("demo", "primary")
    later = utcnow() + timedelta(seconds=1)
    with ctx.sessionmaker() as session:
        sl_id = copy_row(ctx, "k/0.dat").storage_location_id
        pages: list[list[str]] = []
        after = 0
        while page := index_db.removal_candidates(session, sl_id, later, after_id=after, limit=3):
            pages.append([c.path for c in page])
            after = page[-1].copy_id
    assert [len(p) for p in pages] == [3, 3, 1]
    assert sorted(p for page in pages for p in page) == [f"k/{i}.dat" for i in range(7)]


SCALE_FILES = int(os.environ.get("HP_SCALE_FILES", "4000"))
"""Tree size of the scale test; ``HP_SCALE_FILES=20000`` runs the full SVC-17 size."""


SCALE_FACTOR = SCALE_FILES / 20000
"""Scales the time bounds, which were measured at 20k files."""


def _no_find(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("the index must stream its listing, not call find")


def _seed(ctx: AppContext, root: Path, paths: list[str]) -> None:
    """Write ``paths`` to the store and record each as an available copy, in bulk."""
    from sqlalchemy import insert

    from heliopuddle.server.database.orm import Series

    store = StorageLocation(f"file://{root}")
    when = (datetime.now(UTC) - OLD).timestamp()
    for path in paths:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.encode())
        os.utime(target, (when, when))
    with ctx.write_session() as session:
        row = models.get_storage_location(session, "demo", "primary")
        assert row is not None
        project_id, sl_id = row.project_id, row.id
        series, files, copies = [], [], []
        for i, path in enumerate(paths, start=1):
            info = store.info(path)
            digest = _md5(path.encode())
            series.append({"id": i, "project_id": project_id, "series_key": path})
            files.append(
                {
                    "id": i,
                    "project_id": project_id,
                    "path": path,
                    "name": path.rsplit("/", 1)[-1],
                    "series_id": i,
                    "size": info.size,
                    "hash": digest,
                    "is_latest": True,
                }
            )
            copies.append(
                {
                    "id": i,
                    "file_id": i,
                    "storage_location_id": sl_id,
                    "state": FileState.AVAILABLE,
                    "size": info.size,
                    "mtime": info.mtime,
                    "hash": digest,
                    "generation": 1,
                    "updated_at": utcnow() - OLD,
                }
            )
        session.execute(insert(Series), series)
        session.execute(insert(File), files)
        session.execute(insert(FileStorageLocation), copies)


@pytest.mark.req("SVC-17")
def test_full_index_at_scale_streams_and_detects_removals_set_free(
    ctx: AppContext, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.projects["demo"].index.batch_size = 1000
    ctx.projects["demo"].index.batch.max_files = None
    paths = [f"d{i % 50:02d}/f{i:05d}.dat" for i in range(SCALE_FILES)]
    _seed(ctx, root, paths)
    gone = paths[::997]
    for path in gone:
        (root / path).unlink()
    from fsspec.implementations.local import LocalFileSystem

    monkeypatch.setattr(LocalFileSystem, "find", _no_find)
    monkeypatch.setattr(index_finish, "_find_expired", lambda *a: [])

    start = time.monotonic()
    listed = sum(1 for _ in StorageLocation(f"file://{root}").list_files())
    list_seconds = time.monotonic() - start
    start = time.monotonic()
    summary = index_full.fn("demo", "primary")
    index_seconds = time.monotonic() - start

    assert listed == len(paths) - len(gone)
    assert (summary.listed, summary.added, summary.changed) == (listed, 0, 0)
    assert (summary.removed, summary.skipped) == (len(gone), 0)
    with ctx.sessionmaker() as session:
        rows = session.execute(
            select(
                File.path,
                File.deleted_at,
                FileStorageLocation.state,
                FileStorageLocation.last_seen_at,
            ).join(FileStorageLocation, FileStorageLocation.file_id == File.id)
        ).all()
    removed = {p for p, deleted_at, state, _ in rows if deleted_at is not None}
    assert removed == set(gone)
    assert all(s == FileState.DELETED for p, _, s, _ in rows if p in removed)
    assert {seen for p, _, _, seen in rows if p not in removed} == {summary.started_at}
    # Bounds about 3x the times measured on a development container at 20k files, scaled
    # with the tree size (SVC-17).
    assert list_seconds < 15 * max(SCALE_FACTOR, 0.2), (
        f"listing {len(paths)} files took {list_seconds:.1f}s"
    )
    assert index_seconds < 45 * max(SCALE_FACTOR, 0.2), (
        f"indexing {len(paths)} files took {index_seconds:.1f}s"
    )


ADOPT_FILES = 300


@pytest.mark.req("SVC-17")
@pytest.mark.slow
def test_adopting_a_tree_is_linear(ctx: AppContext, root: Path) -> None:
    ctx.projects["demo"].index.batch_size = 100
    ctx.projects["demo"].index.batch.max_files = None
    for i in range(ADOPT_FILES):
        put(root, f"a{i % 10}/f{i:04d}.dat", f"{i}".encode())

    start = time.monotonic()
    summary = index_full.fn("demo", "primary")
    seconds = time.monotonic() - start

    assert (summary.listed, summary.added) == (ADOPT_FILES, ADOPT_FILES)
    with ctx.sessionmaker() as session:
        copies = session.scalars(select(FileStorageLocation)).all()
    assert len(copies) == ADOPT_FILES
    assert all(c.state == FileState.AVAILABLE and c.hash for c in copies)
    assert seconds < 20, f"adopting {ADOPT_FILES} files took {seconds:.1f}s"

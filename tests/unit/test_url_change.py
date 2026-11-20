"""Storage URL change: "the disk moved, with its data" (SVC-12).

Each test asserts the DB state after config sync and after the re-checking
full index, and the store contents, not only counts.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, update

from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import File, FileStorageLocation, IndexProgress, utcnow
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services import index_finish
from heliopuddle.server.services.index import index_full
from heliopuddle.server.start import StartupError, ensure_storage_roots

pytestmark = pytest.mark.usefixtures("inline_prefect")

OLD = timedelta(hours=2)


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def put(root: Path, path: str, data: bytes) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    when = (datetime.now(UTC) - OLD).timestamp()
    os.utime(target, (when, when))


def copies(ctx: AppContext, location: str) -> dict[str, FileStorageLocation]:
    with ctx.sessionmaker() as session:
        rows = session.execute(
            select(File.path, FileStorageLocation)
            .join(FileStorageLocation, FileStorageLocation.file_id == File.id)
            .where(FileStorageLocation.storage_location.has(name=location))
        ).all()
        for _, row in rows:
            session.expunge(row)
        return {path: row for path, row in rows}


def files(ctx: AppContext) -> dict[str, File]:
    with ctx.sessionmaker() as session:
        rows = session.scalars(select(File)).all()
        for row in rows:
            session.expunge(row)
        return {row.path: row for row in rows}


def progress(ctx: AppContext) -> list[IndexProgress]:
    with ctx.sessionmaker() as session:
        return list(session.scalars(select(IndexProgress)))


def move_to(ctx: AppContext, location: str, new_root: Path) -> None:
    """Point a storage location at ``new_root`` in config and sync it (as on start)."""
    cfg = ctx.storage_location_config("demo", location)
    assert cfg is not None
    cfg.url = f"file://{new_root}"
    ctx.sync_config()


@pytest.fixture(autouse=True)
def _no_expired_operations(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(index_finish, "_find_expired", lambda *a: [])


@pytest.fixture
def adopted(ctx: AppContext, store: Path) -> dict[str, bytes]:
    """The primary storage location, fully indexed, with copies in every state."""
    content = {
        "m/a.dat": b"alpha",
        "m/b.dat": b"bravo",
        "m/over.dat": b"old content",
        "m/gone.dat": b"golf",
        "m/del.dat": b"delta",
    }
    for path, data in content.items():
        put(store, path, data)
    index_full.fn("demo", "primary")
    rows = copies(ctx, "primary")
    with ctx.write_session() as session:
        # An overwrite in flight: old content still served, new content staged.
        session.execute(
            update(FileStorageLocation)
            .where(FileStorageLocation.id == rows["m/over.dat"].id)
            .values(
                state=FileState.OVERWRITING,
                op_token=str(uuid.uuid4()),
                op_deadline=utcnow() + timedelta(hours=1),
                pending_hash=_md5(b"new content"),
                pending_size=11,
            )
        )
        session.execute(
            update(FileStorageLocation)
            .where(FileStorageLocation.id == rows["m/gone.dat"].id)
            .values(state=FileState.MISSING)
        )
        session.execute(
            update(FileStorageLocation)
            .where(FileStorageLocation.id == rows["m/del.dat"].id)
            .values(state=FileState.DELETING)
        )
    return content


@pytest.mark.req("SVC-12")
def test_url_change_resets_copies_for_a_recheck(
    ctx: AppContext,
    store: Path,
    tmp_path: Path,
    adopted: dict[str, bytes],
    caplog: pytest.LogCaptureFixture,
) -> None:
    before = copies(ctx, "primary")
    assert progress(ctx)
    new_root = tmp_path / "moved"
    shutil.copytree(store, new_root)

    with caplog.at_level(logging.WARNING):
        move_to(ctx, "primary", new_root)

    warning = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warning) == 1
    message = warning[0].getMessage()
    assert str(store) in message and str(new_root) in message and "re-check" in message
    after = copies(ctx, "primary")
    for path, old in before.items():
        new = after[path]
        expected = FileState.DELETING if path == "m/del.dat" else FileState.PENDING
        assert new.state == expected, path
        assert (new.hash, new.size) == (old.hash, old.size), path
        assert (new.mtime, new.last_seen_at, new.op_token, new.op_deadline) == (None,) * 4
        assert (new.pending_hash, new.pending_size) == (None, None)
        assert new.generation == old.generation + 1
    assert progress(ctx) == []
    latest = {p for p, f in files(ctx).items() if f.is_latest}
    assert latest == set(before) - {"m/del.dat"}  # token-less pending is latest-eligible

    # Syncing again with the same URL changes nothing.
    move_to(ctx, "primary", new_root)
    assert {p: c.generation for p, c in copies(ctx, "primary").items()} == {
        p: c.generation for p, c in after.items()
    }


@pytest.mark.req("SVC-12")
def test_moved_disk_is_rechecked_to_available(
    ctx: AppContext, store: Path, tmp_path: Path, adopted: dict[str, bytes]
) -> None:
    hashes = {path: f.hash for path, f in files(ctx).items()}
    new_root = tmp_path / "moved"
    shutil.copytree(store, new_root)  # mtimes differ: only content counts
    move_to(ctx, "primary", new_root)

    summary = index_full.fn("demo", "primary")

    assert (summary.added, summary.changed, summary.removed, summary.unknown) == (0, 0, 0, 0)
    assert summary.reverified == 4
    rows = copies(ctx, "primary")
    for path in ("m/a.dat", "m/b.dat", "m/over.dat", "m/gone.dat"):
        assert rows[path].state == FileState.AVAILABLE, path
        assert rows[path].hash == hashes[path] == _md5(adopted[path])
        assert rows[path].mtime is not None and rows[path].last_seen_at == summary.started_at
    assert rows["m/del.dat"].state == FileState.DELETING  # retried by repair later
    assert {path: f.hash for path, f in files(ctx).items()} == hashes
    assert (new_root / "m/over.dat").read_bytes() == b"old content"
    assert [p.last_full_at for p in progress(ctx)] == [summary.started_at]


@pytest.mark.req("SVC-12")
def test_wrong_new_root_marks_differences(
    ctx: AppContext, store: Path, tmp_path: Path, adopted: dict[str, bytes]
) -> None:
    new_root = tmp_path / "other"
    put(new_root, "m/a.dat", b"alpha")  # same
    put(new_root, "m/b.dat", b"something else")  # different
    put(new_root, "m/new.dat", b"new")  # not known yet
    # m/over.dat and m/gone.dat are absent
    move_to(ctx, "primary", new_root)

    summary = index_full.fn("demo", "primary")

    rows = copies(ctx, "primary")
    assert rows["m/a.dat"].state == FileState.AVAILABLE
    assert rows["m/b.dat"].state == FileState.UNKNOWN
    assert rows["m/new.dat"].state == FileState.AVAILABLE
    assert rows["m/over.dat"].state == rows["m/gone.dat"].state == FileState.DELETED
    by_path = files(ctx)
    assert by_path["m/b.dat"].hash == _md5(b"bravo")  # never accepted as new content
    assert not by_path["m/b.dat"].is_latest
    assert by_path["m/over.dat"].deleted_at is not None
    assert (summary.added, summary.removed, summary.unknown) == (1, 2, 1)
    assert (store / "m/b.dat").read_bytes() == b"bravo"  # the old root is never touched


@pytest.mark.req("SVC-12")
def test_wrong_root_of_non_accepting_location_marks_missing(
    ctx: AppContext, tmp_path: Path
) -> None:
    old_root = tmp_path / "store2"
    put(old_root, "n/a.dat", b"a")
    put(old_root, "n/b.dat", b"b")
    index_full.fn("demo", "secondary")  # unknown copies (does not accept changes)
    new_root = tmp_path / "store2-new"
    put(new_root, "n/a.dat", b"a")
    move_to(ctx, "secondary", new_root)

    summary = index_full.fn("demo", "secondary")

    rows = copies(ctx, "secondary")
    assert rows["n/a.dat"].state == FileState.AVAILABLE  # content matches the file
    assert rows["n/b.dat"].state == FileState.MISSING
    assert (summary.missing, summary.reverified) == (1, 1)
    assert files(ctx)["n/b.dat"].deleted_at is None


@pytest.mark.req("SVC-12")
def test_unusable_new_root_fails_startup(ctx: AppContext, tmp_path: Path) -> None:
    cfg = ctx.storage_location_config("demo", "secondary")
    assert cfg is not None and cfg.create_root is False
    move_to(ctx, "secondary", tmp_path / "does-not-exist")

    with pytest.raises(StartupError, match="does-not-exist"):
        ensure_storage_roots(ctx)

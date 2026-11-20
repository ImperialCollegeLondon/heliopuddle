"""Index hardening: watermark, observations and guarded writes."""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import update

from heliopuddle.server import models
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import (
    FileStorageLocation,
    utcnow,
)
from heliopuddle.server.models import index as index_db
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services import index as index_mod
from heliopuddle.server.services import index_diff, index_finish
from heliopuddle.server.services.index import (
    IndexEntry,
    index_batch,
    index_full,
    index_incremental,
)
from heliopuddle.storage import StorageLocation
from tests.unit._index_hardening_helpers import (
    OLD,
    _md5,
    copy_row,
    file_row,
    put,
    root,
    set_copy,
)

_FIXTURES = (root,)  # referenced so ruff sees the imports used

pytestmark = pytest.mark.usefixtures("inline_prefect")


def assert_copy_matches_store(ctx: AppContext, root: Path, path: str) -> None:
    """Size, mtime and hash stored on the copy describe the object in the store."""
    copy = copy_row(ctx, path)
    info = StorageLocation(f"file://{root}").info(path)
    assert (copy.size, copy.mtime) == (info.size, info.mtime)
    assert copy.hash == _md5((root / path).read_bytes())
    assert file_row(ctx, path).hash == copy.hash


# --- SVC-14: the incremental watermark ------------------------------------------------


@pytest.mark.req("SVC-14")
def test_settling_file_is_indexed_by_a_later_incremental_run(
    ctx: AppContext, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = ctx.projects["demo"].index
    cfg.settle_time = timedelta(minutes=10)
    cfg.incremental_overlap = timedelta(minutes=1)  # less than the settle time
    put(root, "w/settling.dat", b"just written", age=timedelta(minutes=2))

    first = index_incremental.fn("demo", "primary")
    assert (first.listed, first.skipped, first.added) == (1, 1, 0)
    with ctx.sessionmaker() as session:
        assert models.get_file_by_path(session, "demo", "w/settling.dat") is None
        progress = index_db.get_index_progress(session, "demo", "primary")
        assert progress is not None and first.started_at is not None
        assert progress.last_incremental_at == first.started_at - cfg.settle_time

    # Fifteen minutes later the file has settled; it is after the watermark
    # minus the overlap, so this run processes it.
    later = utcnow() + timedelta(minutes=15)
    monkeypatch.setattr(index_mod, "utcnow", lambda: later)
    second = index_incremental.fn("demo", "primary")

    assert second.added == 1
    assert_copy_matches_store(ctx, root, "w/settling.dat")


# --- SVC-16: one observation per object -----------------------------------------------


@pytest.mark.req("SVC-16")
def test_new_file_changing_while_hashed_is_skipped_then_indexed_consistently(
    ctx: AppContext, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    put(root, "c/new.dat", b"first version")
    real_md5 = StorageLocation.md5
    calls = {"n": 0}

    def writer_appends(self: StorageLocation, path: str) -> str:
        digest = real_md5(self, path)
        calls["n"] += 1
        if calls["n"] == 1:  # a writer finishes the file while hp is hashing it
            put(root, path, b"first version, then more", age=OLD - timedelta(minutes=1))
        return digest

    monkeypatch.setattr(StorageLocation, "md5", writer_appends)
    first = index_full.fn("demo", "primary")

    assert (first.listed, first.added, first.skipped) == (1, 0, 1)
    with ctx.sessionmaker() as session:
        assert models.get_file_by_path(session, "demo", "c/new.dat") is None

    second = index_full.fn("demo", "primary")
    assert second.added == 1
    assert_copy_matches_store(ctx, root, "c/new.dat")
    assert file_row(ctx, "c/new.dat").hash == _md5(b"first version, then more")


@pytest.mark.req("SVC-16")
def test_known_file_changing_while_hashed_keeps_the_old_observation(
    ctx: AppContext, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    put(root, "c/known.dat", b"v1")
    index_full.fn("demo", "primary")
    before = copy_row(ctx, "c/known.dat")
    put(root, "c/known.dat", b"v2 longer", age=OLD - timedelta(minutes=5))
    real_md5 = StorageLocation.md5

    def changes_again(self: StorageLocation, path: str) -> str:
        digest = real_md5(self, path)
        put(root, path, b"v3 even longer", age=OLD - timedelta(minutes=6))
        return digest

    monkeypatch.setattr(StorageLocation, "md5", changes_again)
    summary = index_full.fn("demo", "primary")

    assert (summary.changed, summary.skipped, summary.removed) == (0, 1, 0)
    after = copy_row(ctx, "c/known.dat")
    assert (after.size, after.mtime, after.hash, after.generation) == (
        before.size,
        before.mtime,
        before.hash,
        before.generation,
    )
    assert after.last_seen_at == summary.started_at  # seen, so never taken for removed
    assert file_row(ctx, "c/known.dat").hash == _md5(b"v1")

    monkeypatch.undo()
    assert index_full.fn("demo", "primary").changed == 1
    assert_copy_matches_store(ctx, root, "c/known.dat")
    assert copy_row(ctx, "c/known.dat").generation > before.generation


@pytest.mark.req("SVC-16")
def test_entry_not_matching_its_listing_is_skipped(ctx: AppContext, root: Path) -> None:
    put(root, "c/stale.dat", b"now longer than listed")
    listed = IndexEntry(path="c/stale.dat", size=3, mtime=utcnow() - OLD)

    counts = index_batch.fn("demo", "primary", [listed])

    assert (counts.added, counts.skipped) == (0, 1)
    with ctx.sessionmaker() as session:
        assert models.get_file_by_path(session, "demo", "c/stale.dat") is None


@pytest.mark.req("SVC-16")
def test_listing_md5_is_one_observation(ctx: AppContext, root: Path) -> None:
    put(root, "c/native.dat", b"content")
    mtime = StorageLocation(f"file://{root}").info("c/native.dat").mtime
    entry = IndexEntry(path="c/native.dat", size=7, mtime=mtime, md5="AB" * 16)

    assert index_batch.fn("demo", "primary", [entry]).added == 1

    copy = copy_row(ctx, "c/native.dat")
    assert (copy.size, copy.mtime, copy.hash) == (7, mtime, "ab" * 16)


# --- SVC-16: ownership and guarded writes ---------------------------------------------


@pytest.mark.req("SVC-16")
@pytest.mark.parametrize(
    ("state", "token", "expires_in"),
    [
        (FileState.PENDING, True, timedelta(hours=1)),  # an upload is writing it
        (FileState.AVAILABLE, True, timedelta(hours=1)),  # owned, whatever the state
        (FileState.OVERWRITING, True, timedelta(hours=1)),
        (FileState.DELETING, True, timedelta(hours=1)),
        (FileState.DELETING, False, None),  # a failed delete, left for repair
        (FileState.PENDING, True, -timedelta(hours=1)),  # expired: left for repair
    ],
)
def test_owned_and_in_flight_copies_are_neither_diffed_nor_removed(
    ctx: AppContext,
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: FileState,
    token: bool,
    expires_in: timedelta | None,
) -> None:
    put(root, "o/listed.dat", b"listed")
    put(root, "o/absent.dat", b"absent")
    index_full.fn("demo", "primary")
    (root / "o/absent.dat").unlink()
    put(root, "o/listed.dat", b"changed in the store", age=OLD - timedelta(minutes=1))
    values: dict[str, Any] = {"state": state}
    if token:
        assert expires_in is not None
        values |= {"op_token": str(uuid.uuid4()), "op_deadline": utcnow() + expires_in}
    for path in ("o/listed.dat", "o/absent.dat"):
        if token:
            values["op_token"] = str(uuid.uuid4())
        set_copy(ctx, path, **values)
    before = {p: copy_row(ctx, p) for p in ("o/listed.dat", "o/absent.dat")}
    monkeypatch.setattr(index_finish, "_find_expired", lambda *a: [])

    summary = index_full.fn("demo", "primary")

    assert (summary.changed, summary.removed, summary.missing, summary.added) == (0, 0, 0, 0)
    for path, row in before.items():
        after = copy_row(ctx, path)
        assert (after.state, after.generation, after.hash, after.op_token) == (
            row.state,
            row.generation,
            row.hash,
            row.op_token,
        )
        assert file_row(ctx, path).deleted_at is None
    assert file_row(ctx, "o/listed.dat").hash == _md5(b"listed")


@pytest.mark.req("SVC-16")
def test_copy_changed_between_read_and_write_is_skipped(
    ctx: AppContext, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    put(root, "g/a.dat", b"one")
    index_full.fn("demo", "primary")
    put(root, "g/a.dat", b"two!", age=OLD - timedelta(minutes=1))
    real = index_diff.observe  # pyright: ignore[reportPrivateUsage]

    def someone_else_changes_it(
        store: StorageLocation, entry: IndexEntry, *, read: bool = False
    ) -> index_diff.Observation | None:
        with ctx.write_session() as session:  # e.g. a verify finishing meanwhile
            session.execute(
                update(FileStorageLocation).values(generation=FileStorageLocation.generation + 1)
            )
        return real(store, entry, read=read)

    monkeypatch.setattr(index_diff, "observe", someone_else_changes_it)
    summary = index_full.fn("demo", "primary")

    assert (summary.changed, summary.skipped) == (0, 1)
    assert file_row(ctx, "g/a.dat").hash == _md5(b"one")
    assert copy_row(ctx, "g/a.dat").hash == _md5(b"one")


@pytest.mark.req("SVC-16")
def test_index_changes_bump_the_generation(ctx: AppContext, root: Path) -> None:
    put(root, "g/b.dat", b"b")
    index_full.fn("demo", "primary")
    created = copy_row(ctx, "g/b.dat").generation
    assert created >= 1

    index_full.fn("demo", "primary")  # unchanged: only last_seen_at
    assert copy_row(ctx, "g/b.dat").generation == created

    put(root, "g/b.dat", b"b changed", age=OLD - timedelta(minutes=1))
    index_full.fn("demo", "primary")
    changed = copy_row(ctx, "g/b.dat").generation
    assert changed == created + 1

    (root / "g/b.dat").unlink()
    index_full.fn("demo", "primary")
    removed = copy_row(ctx, "g/b.dat")
    assert (removed.state, removed.generation) == (FileState.DELETED, changed + 1)


@pytest.mark.req("SVC-16")
def test_new_file_hash_bumps_other_copies_but_not_owned_ones(
    ctx: AppContext, root: Path, tmp_path: Path
) -> None:
    store2 = tmp_path / "store2"
    put(root, "h/a.dat", b"old")
    put(store2, "h/a.dat", b"old")
    index_full.fn("demo", "primary")
    index_full.fn("demo", "secondary")
    set_copy(ctx, "h/a.dat", state=FileState.AVAILABLE)  # pretend it was accepted
    secondary = copy_row(ctx, "h/a.dat", "secondary")
    with ctx.write_session() as session:
        session.execute(
            update(FileStorageLocation)
            .where(FileStorageLocation.id == secondary.id)
            .values(state=FileState.AVAILABLE)
        )
    put(root, "h/a.dat", b"new content", age=OLD - timedelta(minutes=1))

    index_full.fn("demo", "primary")

    other = copy_row(ctx, "h/a.dat", "secondary")
    assert other.state == FileState.UNKNOWN
    assert other.generation == secondary.generation + 1
    assert file_row(ctx, "h/a.dat").hash == _md5(b"new content")

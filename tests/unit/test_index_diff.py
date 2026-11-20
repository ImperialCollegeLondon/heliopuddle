"""Index flow: the diff table."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from heliopuddle.server import models
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import FileStorageLocation
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services.index import (
    index_full,
)
from heliopuddle.server.services.index_names import SYSTEM_USER
from heliopuddle.storage import StorageLocation
from tests.unit._index_helpers import (  # noqa: F401
    OLD,
    _md5,
    by_path,
    copy_state,
    put,
    register,
    set_index,
    stores,
)

_FIXTURES = (stores,)  # referenced so ruff sees the imports used

pytestmark = pytest.mark.usefixtures("inline_prefect")


def verified(ctx: AppContext, result: models.RegisterResult, root: Path, path: str) -> None:
    """Mark a registered copy available with what is in the store."""
    info = StorageLocation(f"file://{root}").info(path)
    with ctx.write_session() as session:
        models.set_copy_verified(
            session,
            result.copy_id,
            size=info.size,
            mtime=info.mtime,
            hash=_md5((root / path).read_bytes()),
        )


# --- diff table ---------------------------------------------------------------------


@pytest.mark.req("SVC-03")
@pytest.mark.parametrize("batch_size", [1, 3])
@pytest.mark.parametrize("location", ["primary", "secondary"])
def test_new_files_are_added(
    ctx: AppContext, stores: dict[str, Path], location: str, batch_size: int
) -> None:
    set_index(ctx, batch_size=batch_size)
    accept = location == "primary"
    for i in range(4):
        put(stores[location], f"new/f{i}.dat", f"data {i}".encode())

    summary = index_full.fn("demo", location)

    assert (summary.listed, summary.added, summary.skipped) == (4, 4, 0)
    assert summary.unknown == (0 if accept else 4)
    for i in range(4):
        rec = by_path(ctx, f"new/f{i}.dat")
        assert rec.hash == _md5(f"data {i}".encode())
        assert rec.created_by == SYSTEM_USER
        assert copy_state(rec, location) == (FileState.AVAILABLE if accept else FileState.UNKNOWN)
        assert rec.is_latest is accept
        copy = rec.storage_locations[0]
        assert copy.size == len(f"data {i}") and copy.mtime is not None
        assert copy.last_seen_at == summary.started_at
        assert (copy.hash is not None) is accept


@pytest.mark.req("SVC-03")
@pytest.mark.parametrize(
    ("location", "same_content", "expected"),
    [
        ("primary", True, FileState.AVAILABLE),
        ("primary", False, FileState.UNKNOWN),
        ("secondary", True, FileState.UNKNOWN),
    ],
)
def test_live_file_without_copy_here_gets_a_copy(
    ctx: AppContext,
    stores: dict[str, Path],
    location: str,
    same_content: bool,
    expected: FileState,
) -> None:
    other = "secondary" if location == "primary" else "primary"
    put(stores[other], "k/a.dat", b"original")
    verified(ctx, register(ctx, other, "k/a.dat", b"original"), stores[other], "k/a.dat")
    put(stores[location], "k/a.dat", b"original" if same_content else b"different")

    summary = index_full.fn("demo", location)

    assert summary.added == 1
    assert summary.unknown == (1 if expected == FileState.UNKNOWN else 0)
    rec = by_path(ctx, "k/a.dat")
    assert copy_state(rec, location) == expected
    assert copy_state(rec, other) == FileState.AVAILABLE
    assert rec.hash == _md5(b"original")
    assert rec.is_latest


@pytest.mark.req("SVC-03")
@pytest.mark.parametrize("location", ["primary", "secondary"])
def test_unchanged_file_only_updates_last_seen(
    ctx: AppContext, stores: dict[str, Path], location: str
) -> None:
    put(stores[location], "u/a.dat", b"same")
    first = index_full.fn("demo", location)
    before = by_path(ctx, "u/a.dat")

    second = index_full.fn("demo", location)

    after = by_path(ctx, "u/a.dat")
    assert (second.listed, second.added, second.changed, second.unknown) == (1, 0, 0, 0)
    assert after.storage_locations[0].last_seen_at == second.started_at
    assert first.started_at is not None and second.started_at is not None
    assert second.started_at > first.started_at
    assert after.storage_locations[0].state == before.storage_locations[0].state
    assert after.storage_locations[0].updated_at == before.storage_locations[0].updated_at
    assert after.updated_at == before.updated_at


@pytest.mark.req("SVC-03")
def test_changed_file_accepted_updates_hash_and_size(
    ctx: AppContext, stores: dict[str, Path]
) -> None:
    root = stores["primary"]
    put(root, "c/a.dat", b"v1")
    index_full.fn("demo", "primary")
    put(root, "c/a.dat", b"version two", age=timedelta(hours=1))

    summary = index_full.fn("demo", "primary")

    assert (summary.changed, summary.added, summary.unknown) == (1, 0, 0)
    rec = by_path(ctx, "c/a.dat")
    assert rec.hash == _md5(b"version two")
    assert rec.size == len(b"version two")
    assert rec.updated_by == SYSTEM_USER
    assert copy_state(rec, "primary") == FileState.AVAILABLE
    assert rec.storage_locations[0].hash == _md5(b"version two")
    assert rec.is_latest

    # A touch (same content, new mtime) is recorded without counting as a change.
    put(root, "c/a.dat", b"version two", age=timedelta(minutes=30))
    touched = index_full.fn("demo", "primary")
    assert touched.changed == 0
    assert by_path(ctx, "c/a.dat").storage_locations[0].mtime == datetime.fromtimestamp(
        (root / "c/a.dat").stat().st_mtime, UTC
    )


@pytest.mark.req("SVC-03")
def test_changed_file_not_accepted_becomes_unknown(
    ctx: AppContext, stores: dict[str, Path]
) -> None:
    root = stores["secondary"]
    put(root, "c/b.dat", b"v1")
    result = register(ctx, "secondary", "c/b.dat", b"v1")
    verified(ctx, result, root, "c/b.dat")
    put(root, "c/b.dat", b"changed!", age=timedelta(hours=1))

    summary = index_full.fn("demo", "secondary")

    assert (summary.unknown, summary.changed) == (1, 0)
    rec = by_path(ctx, "c/b.dat")
    assert rec.hash == _md5(b"v1")
    assert copy_state(rec, "secondary") == FileState.UNKNOWN
    assert not rec.is_latest
    # Seen again unchanged, it stays unknown and is not counted twice.
    again = index_full.fn("demo", "secondary")
    assert again.unknown == 0
    assert copy_state(by_path(ctx, "c/b.dat"), "secondary") == FileState.UNKNOWN


@pytest.mark.req("SVC-03")
def test_removed_file_accepted_soft_deletes_and_previous_version_is_latest(
    ctx: AppContext, stores: dict[str, Path]
) -> None:
    root = stores["primary"]
    put(root, "r/x_v1.dat", b"one")
    put(root, "r/x_v2.dat", b"two")
    index_full.fn("demo", "primary")
    assert by_path(ctx, "r/x_v2.dat").is_latest
    (root / "r/x_v2.dat").unlink()

    summary = index_full.fn("demo", "primary")

    assert (summary.removed, summary.missing) == (1, 0)
    gone = by_path(ctx, "r/x_v2.dat", include_deleted=True)
    assert gone.deleted_at is not None
    assert gone.updated_by == SYSTEM_USER
    assert copy_state(gone, "primary") == FileState.DELETED
    assert not gone.is_latest
    assert by_path(ctx, "r/x_v1.dat").is_latest


@pytest.mark.req("SVC-03")
def test_removed_file_with_other_copies_stays_live(
    ctx: AppContext, stores: dict[str, Path]
) -> None:
    put(stores["primary"], "r/y.dat", b"y")
    put(stores["secondary"], "r/y.dat", b"y")
    index_full.fn("demo", "primary")
    index_full.fn("demo", "secondary")
    (stores["primary"] / "r/y.dat").unlink()

    assert index_full.fn("demo", "primary").removed == 1

    rec = by_path(ctx, "r/y.dat")
    assert rec.deleted_at is None
    assert copy_state(rec, "primary") == FileState.DELETED
    assert copy_state(rec, "secondary") == FileState.UNKNOWN
    assert not rec.is_latest

    # It comes back: the deleted copy is treated as no copy and re-added.
    put(stores["primary"], "r/y.dat", b"y")
    back = index_full.fn("demo", "primary")
    assert back.added == 1
    rec = by_path(ctx, "r/y.dat")
    assert copy_state(rec, "primary") == FileState.AVAILABLE
    assert rec.is_latest


@pytest.mark.req("SVC-03")
def test_removed_file_not_accepted_is_missing_and_can_return(
    ctx: AppContext, stores: dict[str, Path]
) -> None:
    root = stores["secondary"]
    put(root, "r/z_v1.dat", b"one")
    put(root, "r/z_v2.dat", b"two")
    for path, data in (("r/z_v1.dat", b"one"), ("r/z_v2.dat", b"two")):
        verified(ctx, register(ctx, "secondary", path, data), root, path)
    assert by_path(ctx, "r/z_v2.dat").is_latest
    (root / "r/z_v2.dat").unlink()

    summary = index_full.fn("demo", "secondary")

    assert (summary.missing, summary.removed) == (1, 0)
    rec = by_path(ctx, "r/z_v2.dat")
    assert rec.deleted_at is None
    assert copy_state(rec, "secondary") == FileState.MISSING
    assert by_path(ctx, "r/z_v1.dat").is_latest
    # Missing copies are not reported again.
    assert index_full.fn("demo", "secondary").missing == 0

    # It reappears: recorded as unknown (external changes are not accepted).
    put(root, "r/z_v2.dat", b"two")
    back = index_full.fn("demo", "secondary")
    assert back.unknown == 1
    assert copy_state(by_path(ctx, "r/z_v2.dat"), "secondary") == FileState.UNKNOWN


@pytest.mark.req("SVC-03")
def test_missing_copy_reappearing_in_accepting_location_is_available(
    ctx: AppContext, stores: dict[str, Path]
) -> None:
    root = stores["primary"]
    put(root, "m/a.dat", b"a")
    index_full.fn("demo", "primary")
    with ctx.write_session() as session:
        copy_id = session.scalars(select(FileStorageLocation.id)).one()
        models.set_copy_state(session, copy_id, FileState.MISSING)
    summary = index_full.fn("demo", "primary")
    assert summary.changed == 0
    assert copy_state(by_path(ctx, "m/a.dat"), "primary") == FileState.AVAILABLE

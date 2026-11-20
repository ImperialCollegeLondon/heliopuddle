"""Adversarial lifecycle: storage URL changes and orphaned temporary files."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from heliopuddle.server.context import AppContext
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services import verify as verify_mod
from heliopuddle.server.services.index import index_full
from heliopuddle.server.services.upload import RepairResult, repair_expired
from heliopuddle.server.services.verify import verify_file
from heliopuddle.storage import StorageLocation
from tests.adversarial import (
    Clock,
    assert_invariants,
    client,
    clock,
    config_dir,  # noqa: F401 - fixture
    protocol,
    put,
    store,
)
from tests.lifecycle import md5, patch_upload, store_bytes, temp_files, upload_attr
from tests.unit._adversarial_lifecycle_helpers import (
    NEW,
    OLD,
    PATH,
    fast_index,
    reserve,
    rows,
    upload,
)

_FIXTURES = (
    client,
    clock,
    fast_index,
    protocol,
)  # referenced so ruff sees the imports used

pytestmark = pytest.mark.usefixtures("inline_prefect")


# --- storage URL change with operations in flight (SVC-12) -----------------------------


def _move(ctx: AppContext, protocol: str, tag: str) -> StorageLocation:
    cfg = ctx.storage_location_config("demo", "primary")
    assert cfg is not None
    cfg.url = f"{protocol}://hp-moved-{tag}"
    ctx.sync_config()
    return store(ctx)


@pytest.mark.req("SVC-12")
def test_url_change_during_an_overwrite_makes_the_client_complete_fail_safely(
    ctx: AppContext, client: TestClient, fast_index: None, protocol: str
) -> None:
    """The disk moved while an overwrite was staged: the token is stale (409) and the
    moved original is re-adopted by the first index; the staged object is never indexed.
    """
    loc = store(ctx)
    upload(client, loc, OLD)
    a = reserve(client, NEW, overwrite=True).json()
    put(loc, a["write_path"], NEW)
    new = _move(ctx, protocol, "ow")
    put(new, PATH, OLD)
    put(new, a["write_path"], NEW)  # the staged file moved with the disk

    assert client.post(f"/uploads/{a['token']}/complete").status_code == 409
    (row,) = rows(ctx)
    assert (row.state, row.token) == (FileState.PENDING, None)

    index_full.fn("demo", "primary")

    (row,) = rows(ctx)
    assert (row.state, row.token, row.file_hash) == (FileState.AVAILABLE, None, md5(OLD))
    assert store_bytes(new, PATH) == OLD
    assert_invariants(ctx, allow_temp=True)
    assert [f.path for f in new.list_files() if not f.path.endswith(".hp-tmp")] == [PATH]


@pytest.mark.req("SVC-12")
def test_url_change_during_a_new_upload_then_the_index_adopts_what_was_written(
    ctx: AppContext, client: TestClient, fast_index: None, protocol: str
) -> None:
    loc = store(ctx)
    a = reserve(client, OLD).json()
    new = _move(ctx, protocol, "new")
    put(new, a["write_path"], OLD)  # the client wrote into the new root

    assert client.post(f"/uploads/{a['token']}/complete").status_code == 409
    index_full.fn("demo", "primary")

    (row,) = rows(ctx)
    assert (row.state, row.token, row.file_hash) == (FileState.AVAILABLE, None, md5(OLD))
    assert_invariants(ctx)
    assert store_bytes(loc, PATH) is None


@pytest.mark.req("SVC-12")
def test_verify_stalled_across_a_url_change_is_dropped(
    ctx: AppContext, client: TestClient, protocol: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    loc = store(ctx)
    upload(client, loc, OLD)
    copy_id = rows(ctx)[0].copy_id
    state = {"fired": False}
    real = verify_mod.observe

    def observe(store_: StorageLocation, path: str) -> Any:
        result = real(store_, path)
        if not state["fired"]:
            state["fired"] = True
            _move(ctx, protocol, "verify")
        return result

    monkeypatch.setattr(verify_mod, "observe", observe)
    verify_file.fn(copy_id)

    (row,) = rows(ctx)
    assert row.state == FileState.PENDING  # awaiting the re-check in the new root
    assert_invariants(ctx)


@pytest.mark.req("SVC-12")
def test_repair_after_a_url_change_never_resurrects_an_old_reservation(
    ctx: AppContext, client: TestClient, clock: Clock, protocol: str
) -> None:
    loc = store(ctx)
    a = reserve(client, OLD).json()
    put(loc, a["write_path"], OLD)
    _move(ctx, protocol, "repair")
    clock.advance(timedelta(hours=2))
    assert repair_expired(rows(ctx)[0].copy_id) == RepairResult.NOT_OWNED_OR_LIVE
    assert_invariants(ctx)


# --- orphaned temporary files are repaired by the full index (STO-10) -------------------


@pytest.mark.req("STO-10")
def test_full_index_removes_a_backup_left_behind_by_a_successful_publish(
    ctx: AppContext,
    client: TestClient,
    fast_index: None,
    protocol: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The publish succeeded but deleting the backup failed (non-replacing backend).

    The copy is ``available`` and ownerless, so ``repair_expired`` never sees
    it. operations.md: "A backup left behind after a successful publish ...
    has no owner; the index's orphan sweep removes it", and STO-10: "orphaned
    staged and backup files are repaired by the full index". After a full
    index no ``*.hp-tmp`` object may remain.
    """
    if protocol == "memory":
        pytest.skip("a backup exists only where a move does not replace")
    loc = store(ctx)
    upload(client, loc, OLD)
    a = reserve(client, NEW, overwrite=True).json()
    put(loc, a["write_path"], NEW)
    real_rm = upload_attr("_rm")  # pyright: ignore[reportPrivateUsage]

    def rm(store_: StorageLocation, path: str) -> None:
        if path.endswith(".hp-old.hp-tmp"):
            raise OSError("injected: backup removal failed")
        real_rm(store_, path)

    patch_upload(monkeypatch, "_rm", rm)
    assert client.post(f"/uploads/{a['token']}/complete").status_code == 200
    patch_upload(monkeypatch, "_rm", real_rm)
    assert len(temp_files(loc)) == 1

    index_full.fn("demo", "primary")

    assert temp_files(loc) == [], "the backup of a finished overwrite is never swept"
    assert store_bytes(loc, PATH) == NEW
    assert_invariants(ctx)


@pytest.mark.req("STO-10")
def test_full_index_removes_a_staged_file_written_by_a_client_after_its_reservation_expired(
    ctx: AppContext, client: TestClient, clock: Clock, fast_index: None
) -> None:
    """An overwrite expired and was repaired; the zombie client then finishes its staging file.

    Nothing owns the staging path any more, no repair will look at it
    (``temp_files`` is only consulted for a token-holding copy), and its
    complete is refused. "Orphaned staged ... files are repaired by the full
    index" (STO-10): after a full index the staging file must be gone.
    """
    loc = store(ctx)
    upload(client, loc, OLD)
    a = reserve(client, NEW, overwrite=True).json()
    clock.advance(timedelta(hours=2))
    assert repair_expired(rows(ctx)[0].copy_id) == RepairResult.REPAIRED
    put(loc, a["write_path"], NEW)  # the zombie's late write
    assert client.post(f"/uploads/{a['token']}/complete").status_code == 409

    index_full.fn("demo", "primary")

    assert temp_files(loc) == [], "a staged file with no owner is never swept"
    assert store_bytes(loc, PATH) == OLD
    assert_invariants(ctx)

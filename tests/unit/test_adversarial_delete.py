"""Adversarial lifecycle: deletes racing other operations and verifies."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from heliopuddle.server import models
from heliopuddle.server.context import AppContext
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services import delete as delete_mod
from heliopuddle.server.services import verify as verify_mod
from heliopuddle.server.services.delete import delete_file
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
from tests.lifecycle import copies, md5, store_bytes, temp_files
from tests.unit._adversarial_lifecycle_helpers import (
    NEW,
    OLD,
    PATH,
    _hooked_info,
    reserve,
    rows,
    upload,
)

_FIXTURES = (
    client,
    clock,
    protocol,
)  # referenced so ruff sees the imports used

pytestmark = pytest.mark.usefixtures("inline_prefect")


NEWER = b"a third version of the content"


# --- delete: the gap between the object check and the store delete (STO-13) -----------


@pytest.mark.req("STO-13")
def test_stale_delete_does_not_remove_content_uploaded_after_its_takeover(
    ctx: AppContext, client: TestClient, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A's delete checks the object against its observation, then stalls before ``rm``.

    Its lease expires; the next reserve repairs the delete (removing the old
    object), a new file is uploaded at the path and published. A resumes and
    calls ``rm``: the guarded DB finish is dropped as stale, but nothing stops
    the store delete, so it removes the *new* file's object. Invariant: a
    stale delete never removes newer content; the new copy is ``available``
    and so its object must still exist with the new bytes.
    """
    loc = store(ctx)
    old = upload(client, loc, OLD)
    interleaved: list[bool] = []

    def hook(self: StorageLocation, path: str) -> None:
        if path != PATH or interleaved:
            return
        interleaved.append(True)
        clock.advance(timedelta(hours=2))
        new = upload(client, loc, NEWER)  # repairs A's delete, then a fresh upload
        assert new["id"] != old["id"]

    monkeypatch.setattr(StorageLocation, "info", _hooked_info(StorageLocation.info, hook))
    delete_file.fn(old["id"], "alice")

    assert interleaved
    assert store_bytes(loc, PATH) == NEWER
    assert_invariants(ctx)


@pytest.mark.req("STO-13")
def test_stale_delete_after_storage_url_change_does_not_remove_an_unobserved_object(
    ctx: AppContext,
    client: TestClient,
    protocol: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The disk "moved with its data" while a delete was in flight.

    The delete began against the old root (its snapshot holds the old
    observation). The URL change makes the copy ``pending`` again with the
    observation cleared and the generation bumped, so the delete is stale. The
    moved object has the same size and mtime, so the delete's check against
    its old snapshot passes: it removes an object hp has not observed in the
    current root ("a delete never removes an object hp has not observed in
    the current root"), while its own DB finish is dropped.
    """
    loc = store(ctx)
    file = upload(client, loc, OLD)
    cfg = ctx.storage_location_config("demo", "primary")
    assert cfg is not None
    new_url = f"{protocol}://hp-moved-{file['id']}"
    moved: list[StorageLocation] = []
    real = delete_mod.delete_copies

    def delete_copies(ctx_: AppContext, project: str, snaps: Any) -> Any:
        cfg.url = new_url
        ctx.sync_config()
        new = store(ctx)
        put(new, PATH, OLD)
        # the same size and mtime, as after copying a disk with its timestamps
        old_obj = loc.fs.store[loc.fs._strip_protocol(loc.full_path(PATH))]  # type: ignore[attr-defined]
        new.fs.store[new.fs._strip_protocol(new.full_path(PATH))].created = old_obj.created  # type: ignore[attr-defined]
        moved.append(new)
        return real(ctx_, project, snaps)

    monkeypatch.setattr(delete_mod, "delete_copies", delete_copies)
    delete_file.fn(file["id"], "alice")

    (new,) = moved
    assert store_bytes(new, PATH) == OLD, "an object never observed in the new root was deleted"


# --- delete vs other operations (held) --------------------------------------------------


@pytest.mark.req("STO-13")
def test_delete_blocks_reserve_and_a_second_delete_while_it_owns_the_copy(
    ctx: AppContext, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    loc = store(ctx)
    file = upload(client, loc, OLD)
    seen: dict[str, Any] = {}

    def hook(self: StorageLocation, path: str) -> None:
        if path != PATH:
            return
        seen["reserve"] = reserve(client, NEW).status_code
        seen["same"] = reserve(client, OLD).status_code
        seen["delete"] = client.delete(f"/files/{file['id']}").status_code

    monkeypatch.setattr(StorageLocation, "info", _hooked_info(StorageLocation.info, hook))
    assert delete_file.fn(file["id"], "alice") == FileState.DELETED
    assert seen == {"reserve": 409, "same": 409, "delete": 409}
    assert store_bytes(loc, PATH) is None
    assert_invariants(ctx)


@pytest.mark.req("STO-13")
def test_delete_of_a_file_with_a_live_overwrite_at_another_location_is_all_or_nothing(
    ctx: AppContext, client: TestClient
) -> None:
    for name in ("primary", "secondary"):
        upload(client, store(ctx, name), OLD, storage_location=name)
    b = reserve(client, NEW, overwrite=True, storage_location="secondary").json()
    put(store(ctx, "secondary"), b["write_path"], NEW)

    r = client.delete("/files/1")

    assert (r.status_code, r.json()["reason"]) == (409, "busy")
    assert [c.state for c in copies(ctx)] == [FileState.AVAILABLE, FileState.OVERWRITING]
    assert store_bytes(store(ctx), PATH) == OLD
    done = client.post(f"/uploads/{b['token']}/complete")
    assert done.status_code == 200
    assert_invariants(ctx)


@pytest.mark.req("STO-13")
def test_delete_repairs_an_expired_overwrite_on_one_copy_then_deletes_everything(
    ctx: AppContext, client: TestClient, clock: Clock
) -> None:
    for name in ("primary", "secondary"):
        upload(client, store(ctx, name), OLD, storage_location=name)
    b = reserve(client, NEW, overwrite=True, storage_location="secondary").json()
    put(store(ctx, "secondary"), b["write_path"], NEW)
    clock.advance(timedelta(hours=2))

    r = client.delete("/files/1")

    assert r.status_code == 200 and r.json()["deleted_at"] is not None
    for name in ("primary", "secondary"):
        assert store_bytes(store(ctx, name), PATH) is None
        assert temp_files(store(ctx, name)) == []
    assert_invariants(ctx)


@pytest.mark.req("STO-13")
def test_crash_between_the_store_delete_and_the_db_finish_is_repaired(
    ctx: AppContext, client: TestClient, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The object is gone but the DB still says ``deleting`` with a live token.

    The path stays busy until the lease expires; then the next reserve repairs
    the delete (an absent object counts as deleted) and a new file can be added.
    """
    loc = store(ctx)
    file = upload(client, loc, OLD)
    real = models.finish_delete
    calls = {"n": 0}

    def crash(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("crash before the DB finish")
        return real(*args, **kwargs)

    monkeypatch.setattr(models, "finish_delete", crash)
    with pytest.raises(RuntimeError):
        delete_file.fn(file["id"], "alice")
    assert store_bytes(loc, PATH) is None
    assert reserve(client, NEW).status_code == 409  # busy: the delete still owns the path
    assert_invariants(ctx)

    clock.advance(timedelta(hours=2))
    assert upload(client, loc, NEW)["hash"] == md5(NEW)
    assert store_bytes(loc, PATH) == NEW
    assert_invariants(ctx)


@pytest.mark.req("SVC-15")
def test_failing_store_delete_leaves_a_retryable_deleting_copy_and_a_live_file(
    ctx: AppContext, client: TestClient, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    loc = store(ctx)
    file = upload(client, loc, OLD)
    real_rm = StorageLocation.rm
    broken = {"on": True}

    def rm(self: StorageLocation, path: str) -> None:
        if broken["on"]:
            raise OSError("store unavailable")
        real_rm(self, path)

    monkeypatch.setattr(StorageLocation, "rm", rm)
    assert delete_file.fn(file["id"], "alice") == FileState.DELETING
    (row,) = rows(ctx)
    assert (row.state, row.token) == (FileState.DELETING, None)
    assert store_bytes(loc, PATH) == OLD
    assert_invariants(ctx)
    # a retry while the store still fails is reported busy, never half-done
    assert reserve(client, NEW).status_code == 409
    assert store_bytes(loc, PATH) == OLD

    broken["on"] = False
    assert upload(client, loc, NEW)["hash"] == md5(NEW)  # repairs the delete first
    assert store_bytes(loc, PATH) == NEW
    assert_invariants(ctx)


# --- verify started before, finishing after (SVC-11) -----------------------------------


@pytest.mark.req("SVC-11")
@pytest.mark.parametrize("competitor", ["overwrite", "delete"])
def test_verify_finishing_after_an_overwrite_or_delete_is_dropped(
    ctx: AppContext,
    client: TestClient,
    competitor: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """verify read the original content and stalls before its guarded write.

    The overwrite publishes (or the delete finishes) meanwhile; verify's
    result is for content that is gone and must not be applied.
    """
    loc = store(ctx)
    file = upload(client, loc, OLD)
    copy_id = rows(ctx)[0].copy_id
    state = {"fired": False}
    real = verify_mod.observe

    def observe(store_: StorageLocation, path: str) -> Any:
        result = real(store_, path)
        if not state["fired"]:
            state["fired"] = True
            if competitor == "overwrite":
                upload(client, loc, NEW, overwrite=True)
            else:
                assert delete_file.fn(file["id"], "alice") == FileState.DELETED
        return result

    monkeypatch.setattr(verify_mod, "observe", observe)
    verify_file.fn(copy_id)

    (row,) = rows(ctx)
    if competitor == "overwrite":
        assert (row.state, row.copy_hash, row.file_hash) == (
            FileState.AVAILABLE,
            md5(NEW),
            md5(NEW),
        )
        assert store_bytes(loc, PATH) == NEW
    else:
        assert row.state == FileState.DELETED
        assert store_bytes(loc, PATH) is None
    assert_invariants(ctx)

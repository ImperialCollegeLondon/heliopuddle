"""Adversarial lifecycle: accept and the index racing uploads and deletes."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from heliopuddle.server import models
from heliopuddle.server.context import AppContext
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services import accept as accept_mod
from heliopuddle.server.services import index_diff
from heliopuddle.server.services.accept import NotUnknownError, accept_copy
from heliopuddle.server.services.delete import delete_file
from heliopuddle.server.services.index import index_full
from heliopuddle.server.services.verify import verify_file
from heliopuddle.storage import StorageLocation
from tests.adversarial import (
    assert_invariants,
    client,
    clock,  # noqa: F401 - fixture
    config_dir,  # noqa: F401 - fixture
    protocol,
    put,
    store,
)
from tests.lifecycle import copies, md5, patch_upload, store_bytes, upload_attr
from tests.unit._adversarial_lifecycle_helpers import (
    NEW,
    OLD,
    PATH,
    _hooked_info,
    fast_index,
    reserve,
    rows,
    upload,
)

_FIXTURES = (
    client,
    fast_index,
    protocol,
)  # referenced so ruff sees the imports used

pytestmark = pytest.mark.usefixtures("inline_prefect")


# --- accept racing the index (SVC-11) --------------------------------------------------


@pytest.mark.req("SVC-11")
def test_accept_loses_to_an_index_that_already_recorded_the_new_content(
    ctx: AppContext, client: TestClient, fast_index: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """accept observed an unknown copy; before it writes, a full index adopts the new content.

    The index (external changes accepted) changes the file's hash and makes
    the copy ``available`` (new generation). accept's guarded write is stale:
    it must be refused (409), and the DB must agree with the store.
    """
    loc = store(ctx)
    file = upload(client, loc, OLD)
    put(loc, PATH, b"changed behind our back")
    verify_file.fn(rows(ctx)[0].copy_id)
    assert rows(ctx)[0].state == FileState.UNKNOWN

    state = {"fired": False}
    real = accept_mod.observe

    def observe(store_: StorageLocation, path: str) -> Any:
        result = real(store_, path)
        if not state["fired"]:
            state["fired"] = True
            put(loc, PATH, b"changed again, by another tool")
            index_full.fn("demo", "primary")
        return result

    monkeypatch.setattr(accept_mod, "observe", observe)
    with pytest.raises(NotUnknownError):
        accept_copy.fn(file["id"], "primary", "alice")
    assert state["fired"]
    assert_invariants(ctx)
    assert rows(ctx)[0].state == FileState.AVAILABLE
    assert store_bytes(loc, PATH) == b"changed again, by another tool"


@pytest.mark.req("SVC-11")
def test_accept_loses_to_a_reservation_on_the_copy(
    ctx: AppContext, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    loc = store(ctx)
    file = upload(client, loc, OLD)
    put(loc, PATH, b"changed behind our back")
    verify_file.fn(rows(ctx)[0].copy_id)
    state = {"fired": False, "reservation": None}
    real = accept_mod.observe

    def observe(store_: StorageLocation, path: str) -> Any:
        result = real(store_, path)
        if not state["fired"]:
            state["fired"] = True
            state["reservation"] = reserve(client, OLD).json()  # same hash: reserve the copy
        return result

    monkeypatch.setattr(accept_mod, "observe", observe)
    with pytest.raises((NotUnknownError, models.CopyBusy)):
        accept_copy.fn(file["id"], "primary", "alice")
    (row,) = rows(ctx)
    assert (row.state, row.file_hash) == (FileState.PENDING, md5(OLD))
    res = state["reservation"]
    put(loc, res["write_path"], OLD)
    assert client.post(f"/uploads/{res['token']}/complete").status_code == 200
    assert_invariants(ctx)


# --- the index while uploads and deletes are in flight ----------------------------------


@pytest.mark.req("SVC-03")
@pytest.mark.parametrize("mode", ["full"])
def test_full_index_leaves_an_in_flight_new_upload_alone(
    ctx: AppContext, client: TestClient, fast_index: None, mode: str
) -> None:
    """The object is half written under a live reservation: the index must not adopt,
    touch or remove it, and the upload still completes.
    """
    loc = store(ctx)
    a = reserve(client, OLD).json()
    put(loc, a["write_path"], OLD[:4])  # a partial object, listed by the index

    summary = index_full.fn("demo", "primary")

    assert summary.added == 0 and summary.removed == 0 and summary.missing == 0
    (row,) = rows(ctx)
    assert (row.state, row.token) == (FileState.PENDING, a["token"])
    put(loc, a["write_path"], OLD)
    assert client.post(f"/uploads/{a['token']}/complete").status_code == 200
    assert_invariants(ctx)


@pytest.mark.req("SVC-03")
def test_full_index_during_the_publish_window_does_not_mark_the_copy_missing(
    ctx: AppContext,
    client: TestClient,
    fast_index: None,
    protocol: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On a non-replacing backend the final path is absent between "original to backup" and
    "staged to final". A full index running exactly then must skip the owned copy.
    """
    loc = store(ctx)
    upload(client, loc, OLD)
    a = reserve(client, NEW, overwrite=True).json()
    put(loc, a["write_path"], NEW)
    real_mv = upload_attr("_mv")  # pyright: ignore[reportPrivateUsage]
    ran: list[Any] = []

    def mv(store_: StorageLocation, src: str, dst: str) -> None:
        if dst == PATH and not ran and not store_.exists(PATH):
            ran.append(index_full.fn("demo", "primary"))
        real_mv(store_, src, dst)

    patch_upload(monkeypatch, "_mv", mv)
    assert client.post(f"/uploads/{a['token']}/complete").status_code == 200

    if protocol == "hpnorepl":
        assert ran, "the window did not occur"
        assert ran[0].removed == 0 and ran[0].missing == 0
    (row,) = rows(ctx)
    assert (row.state, row.file_hash) == (FileState.AVAILABLE, md5(NEW))
    assert store_bytes(loc, PATH) == NEW
    assert_invariants(ctx)


@pytest.mark.req("SVC-03")
def test_full_index_during_a_delete_does_not_touch_the_deleting_copy(
    ctx: AppContext, client: TestClient, fast_index: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    loc = store(ctx)
    file = upload(client, loc, OLD)
    ran: list[Any] = []

    def hook(self: StorageLocation, path: str) -> None:
        if path == PATH:
            ran.append(index_full.fn("demo", "primary"))
            (row,) = rows(ctx)
            assert (row.state, row.token is not None) == (FileState.DELETING, True)

    monkeypatch.setattr(StorageLocation, "info", _hooked_info(StorageLocation.info, hook))
    assert delete_file.fn(file["id"], "alice") == FileState.DELETED
    assert ran and ran[0].removed == 0
    assert_invariants(ctx)


@pytest.mark.req("SVC-03")
def test_index_never_adds_a_copy_to_a_file_deleted_while_it_was_hashing(
    ctx: AppContext, client: TestClient, fast_index: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The index of the secondary found the file's bytes there and is hashing them.

    Meanwhile the file is deleted. The index's guarded write is stale (the
    file is gone), so no copy may be added to the deleted file.
    """
    loc = store(ctx)
    file = upload(client, loc, OLD)
    put(store(ctx, "secondary"), PATH, OLD)
    state = {"fired": False}
    real = index_diff.observe  # pyright: ignore[reportPrivateUsage]

    def observe(*args: Any, **kwargs: Any) -> Any:
        result = real(*args, **kwargs)
        if not state["fired"]:
            state["fired"] = True
            assert delete_file.fn(file["id"], "alice") == FileState.DELETED
        return result

    monkeypatch.setattr(index_diff, "observe", observe)
    index_full.fn("demo", "secondary")

    assert state["fired"]
    assert [(c.storage_location, c.state) for c in copies(ctx)] == [("primary", FileState.DELETED)]
    assert_invariants(ctx)


@pytest.mark.req("SVC-03")
def test_index_adding_a_listed_object_loses_to_a_reservation_of_the_same_path(
    ctx: AppContext, client: TestClient, fast_index: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An object nobody registered is listed and hashed; a client registers that path first.

    The index must back off (exactly one live file at the path, a verified copy).
    """
    loc = store(ctx)
    put(loc, PATH, OLD)
    state = {"fired": False}
    real = index_diff.observe  # pyright: ignore[reportPrivateUsage]

    def observe(*args: Any, **kwargs: Any) -> Any:
        result = real(*args, **kwargs)
        if not state["fired"]:
            state["fired"] = True
            assert upload(client, loc, OLD)["state"] == "available"
        return result

    monkeypatch.setattr(index_diff, "observe", observe)
    index_full.fn("demo", "primary")
    assert state["fired"]
    assert len([c for c in copies(ctx) if not c.deleted]) == 1
    assert_invariants(ctx)

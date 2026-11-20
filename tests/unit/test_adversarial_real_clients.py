"""Adversarial uploads: the real client, and repair versus a late client."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from heliopuddle.server.context import AppContext
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services.upload import RepairResult, repair_expired
from heliopuddle.storage import StorageLocation
from tests.adversarial import (
    Clock,
    assert_invariants,
    client,
    clock,
    config_dir,  # noqa: F401 - fixture
    protocol,  # noqa: F401 - fixture
    put,
    store,
)
from tests.lifecycle import copies, md5, patch_upload, store_bytes, temp_files, upload_attr
from tests.unit._adversarial_uploads_helpers import (
    NEW,
    OLD,
    PATH,
    only_copy,
    reserve,
    upload,
)

_FIXTURES = (
    client,
    clock,
)  # referenced so ruff sees the imports used

# --- the real client ---------------------------------------------------------------------


def _real_client(ctx: AppContext, http: TestClient, user: str = "alice") -> Any:
    from heliopuddle.client import HelioPuddleClient

    return HelioPuddleClient("http://testserver/api/v1", "demo", user=user, http_client=http)


@pytest.mark.req("CLI-08")
def test_two_real_clients_adding_one_path_exactly_one_writes(
    ctx: AppContext, client: TestClient, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B's ``add_file`` runs while A holds the reservation (inside A's write): B gets
    ``BusyError`` and writes nothing; A's bytes and record survive.
    """
    from heliopuddle.client import BusyError

    loc = store(ctx)
    (tmp_path / "a.bin").write_bytes(OLD)
    (tmp_path / "b.bin").write_bytes(NEW)
    seen: dict[str, Any] = {}
    real_write = StorageLocation.write

    def write(self: StorageLocation, path: str, source: Any) -> None:
        if "b_outcome" not in seen:
            seen["b_outcome"] = "running"
            try:
                _real_client(ctx, client, "bob").add_file(tmp_path / "b.bin", PATH)
                seen["b_outcome"] = "wrote"
            except BusyError as e:
                seen["b_outcome"] = e
        real_write(self, path, source)

    monkeypatch.setattr(StorageLocation, "write", write)
    record = _real_client(ctx, client).add_file(tmp_path / "a.bin", PATH)

    assert isinstance(seen["b_outcome"], BusyError)
    assert record.state == "available" and record.hash == md5(OLD)
    assert store_bytes(loc, PATH) == OLD
    assert_invariants(ctx)


@pytest.mark.req("CLI-08")
def test_real_client_source_failure_midway_releases_the_path_and_leaves_no_object(
    ctx: AppContext, client: TestClient, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    loc = store(ctx)
    (tmp_path / "a.bin").write_bytes(OLD)
    real_write = StorageLocation.write

    def failing(self: StorageLocation, path: str, source: Any) -> None:
        put(self, path, OLD[:3])  # partial bytes reached the store
        raise OSError("source failed mid-write")

    monkeypatch.setattr(StorageLocation, "write", failing)
    with pytest.raises(OSError, match="mid-write"):
        _real_client(ctx, client).add_file(tmp_path / "a.bin", PATH)
    monkeypatch.setattr(StorageLocation, "write", real_write)

    assert copies(ctx) == []
    assert store_bytes(loc, PATH) is None
    assert_invariants(ctx)
    # and the path is free again
    record = _real_client(ctx, client).add_file(tmp_path / "a.bin", PATH)
    assert record.state == "available"
    assert_invariants(ctx)


@pytest.mark.req("CLI-08")
def test_real_client_overwrite_failure_keeps_the_original_and_removes_the_staging_file(
    ctx: AppContext, client: TestClient, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    loc = store(ctx)
    (tmp_path / "a.bin").write_bytes(OLD)
    (tmp_path / "n.bin").write_bytes(NEW)
    _real_client(ctx, client).add_file(tmp_path / "a.bin", PATH)
    real_write = StorageLocation.write

    def failing(self: StorageLocation, path: str, source: Any) -> None:
        put(self, path, NEW[:5])
        raise OSError("source failed mid-write")

    monkeypatch.setattr(StorageLocation, "write", failing)
    with pytest.raises(OSError, match="mid-write"):
        _real_client(ctx, client).add_file(tmp_path / "n.bin", PATH, overwrite=True)
    monkeypatch.setattr(StorageLocation, "write", real_write)

    assert store_bytes(loc, PATH) == OLD
    assert temp_files(loc) == []
    (row,) = copies(ctx)
    assert (row.state, row.token, row.file_hash) == (FileState.AVAILABLE, None, md5(OLD))
    assert_invariants(ctx)


# --- repair vs a late client, expiry that nobody acted on ----------------------------------


@pytest.mark.req("STO-09")
@pytest.mark.parametrize("kind", ["new", "overwrite"])
def test_late_client_complete_arriving_during_a_repair_is_refused(
    ctx: AppContext,
    client: TestClient,
    clock: Clock,
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The repair has taken the copy over and is reading the store; the client's complete
    (with the old token) arrives now. It gets 409 and the repair's outcome stands.
    """
    loc = store(ctx)
    if kind == "overwrite":
        upload(client, loc, OLD)
    data = NEW if kind == "overwrite" else OLD
    a = reserve(client, data, overwrite=kind == "overwrite").json()
    put(loc, a["write_path"], data)
    clock.advance(timedelta(hours=2))
    real_observe = upload_attr("observe")
    late: list[int] = []

    def observe(store_: StorageLocation, path: str) -> Any:
        if not late:
            late.append(client.post(f"/uploads/{a['token']}/complete").status_code)
        return real_observe(store_, path)

    patch_upload(monkeypatch, "observe", observe)
    assert repair_expired(only_copy(ctx).copy_id) == RepairResult.REPAIRED

    assert late == [409]
    row = only_copy(ctx)
    assert (row.state, row.token) == (FileState.AVAILABLE, None)
    assert store_bytes(loc, PATH) == OLD  # an unpublished overwrite keeps the original
    assert_invariants(ctx)


@pytest.mark.req("STO-09")
def test_an_expired_reservation_nobody_took_over_can_still_complete_and_renew(
    ctx: AppContext, client: TestClient, clock: Clock
) -> None:
    loc = store(ctx)
    a = reserve(client, OLD).json()
    put(loc, a["write_path"], OLD)
    clock.advance(timedelta(hours=3))
    assert client.post(f"/uploads/{a['token']}/renew").status_code == 200
    clock.advance(timedelta(hours=3))
    done = client.post(f"/uploads/{a['token']}/complete")
    assert done.status_code == 200 and done.json()["state"] == "available"
    assert_invariants(ctx)


@pytest.mark.req("STO-09")
def test_reserving_the_same_content_after_an_expiry_adopts_the_written_object(
    ctx: AppContext, client: TestClient, clock: Clock
) -> None:
    """A wrote exactly the reserved bytes and vanished; B reserves the same content: nothing
    to write (``unchanged``), the object A wrote is the record.
    """
    loc = store(ctx)
    a = reserve(client, OLD).json()
    put(loc, a["write_path"], OLD)
    clock.advance(timedelta(hours=2))
    b = reserve(client, OLD).json()
    assert (b["mode"], b["token"]) == ("unchanged", None)
    assert store_bytes(loc, PATH) == OLD
    assert_invariants(ctx)

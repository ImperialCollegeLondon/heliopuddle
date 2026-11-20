"""Overwrite publication under fault injection at every step (STO-10).

Runs on a replacing fake (``memory://``) and a non-replacing one (a memory
subclass whose ``mv`` refuses an existing destination, registered as a
non-replacing protocol). Each fault is persistent while the upload completes;
then it is lifted, the reservation expires and ``repair_expired`` runs.

Invariants, during the fault and after repair: the final object holds the
complete original or the complete new content (on the non-replacing backend,
a failed publish *and* a failed restore may leave the original only in its
backup until repair); every ``available`` copy's DB hash equals the store's;
after repair the copy is ``available`` and no temporary file is left.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import fsspec
import pytest
from fastapi.testclient import TestClient
from fsspec.implementations.memory import MemoryFileSystem

from heliopuddle.server.api import create_api
from heliopuddle.server.context import AppContext
from heliopuddle.server.models import transitions as tr
from heliopuddle.server.models import uploads as uploads_db
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services.stores import store_for
from heliopuddle.server.services.upload import RepairResult, repair_expired
from heliopuddle.storage import StorageLocation, backends
from heliopuddle.storage.location import backup_path, staging_path
from tests.lifecycle import (
    assert_consistent,
    copies,
    md5,
    patch_upload,
    store_bytes,
    temp_files,
    upload_attr,
)

pytestmark = pytest.mark.req("STO-10")

OLD = b"original content"
NEW = b"the new content, longer"
PATH = "p/data_v1.dat"


class NonReplacingFS(MemoryFileSystem):
    """A memory filesystem whose rename fails when the destination exists (like SMB/SFTP)."""

    protocol = ("hpnorepl",)
    store: dict[str, Any] = {}  # noqa: RUF012 - fsspec's own class-level store
    pseudo_dirs = [""]  # noqa: RUF012

    def mv(self, path1: Any, path2: Any, *args: Any, **kwargs: Any) -> None:
        if self.exists(path2):
            raise FileExistsError(path2)
        super().mv(path1, path2, *args, **kwargs)


fsspec.register_implementation("hpnorepl", NonReplacingFS, clobber=True)


@pytest.fixture(params=["memory", "hpnorepl"])
def protocol(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(
        backends,
        "NON_REPLACING_MOVE_PROTOCOLS",
        backends.NON_REPLACING_MOVE_PROTOCOLS | {"hpnorepl"},
    )
    return request.param


@pytest.fixture
def config_dir(tmp_path: Path, protocol: str) -> Path:
    path = tmp_path / "config"
    path.mkdir()
    root = f"{protocol}://hp-{uuid.uuid4().hex}"
    (path / "demo.yaml").write_text(
        f"project: demo\nstorage_locations:\n  - name: primary\n    url: '{root}'\n"
    )
    return path


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(tr, "clock", c)
    return c


@pytest.fixture
def client(ctx: AppContext) -> Iterator[TestClient]:
    with TestClient(create_api(ctx)) as c:
        yield c


def _store(ctx: AppContext) -> StorageLocation:
    return store_for(ctx, "demo", "primary")


def _put(store: StorageLocation, path: str, data: bytes) -> None:
    store.fs.pipe(store.full_path(path), data)


def _upload(client: TestClient, store: StorageLocation, data: bytes, **extra: Any) -> dict:
    res = client.post(
        "/projects/demo/uploads",
        json={"path": PATH, "hash": md5(data), "size": len(data)} | extra,
    ).json()
    _put(store, res["write_path"], data)
    return res


FAULTS = [
    None,
    "staged_verify",
    "backup_mv",
    "publish_mv",
    "restore",
    "reverify",
    "db_update",
    "backup_rm",
]
AFTER_REPAIR = {
    None: NEW,
    "staged_verify": OLD,
    "backup_mv": OLD,
    "publish_mv": OLD,
    "restore": OLD,
    "reverify": NEW,
    "db_update": NEW,
    "backup_rm": NEW,
}
NON_REPLACING_ONLY = {"backup_mv", "restore", "backup_rm"}


class Faults:
    """Persistent faults injected into the publish step until ``lift``."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, fault: str | None, token: str) -> None:
        self.active = True
        self.fault = fault
        self.staged = staging_path(PATH, token)
        self.backup = backup_path(PATH, token)
        self.real_mv = upload_attr("_mv")
        self.real_observe = upload_attr("observe")
        self.real_rm = upload_attr("_rm")
        self.real_finish = uploads_db.finish_overwrite
        patch_upload(monkeypatch, "_mv", self._mv)
        patch_upload(monkeypatch, "observe", self._observe)
        patch_upload(monkeypatch, "_rm", self._rm)
        monkeypatch.setattr(uploads_db, "finish_overwrite", self._finish)

    def _on(self, *names: str) -> bool:
        return self.active and self.fault in names

    def _mv(self, store: StorageLocation, src: str, dst: str) -> None:
        if self._on("backup_mv") and (src, dst) == (PATH, self.backup):
            raise OSError("injected: backup move")
        if self._on("publish_mv", "restore") and (src, dst) == (self.staged, PATH):
            raise OSError("injected: publish move")
        if self._on("restore") and src == self.backup:
            raise OSError("injected: restore move")
        self.real_mv(store, src, dst)

    def _observe(self, store: StorageLocation, path: str) -> Any:
        if self._on("staged_verify") and path == self.staged:
            raise OSError("injected: staged read")
        if self._on("reverify") and path == PATH:
            raise OSError("injected: final read")
        return self.real_observe(store, path)

    def _rm(self, store: StorageLocation, path: str) -> None:
        if self._on("backup_rm") and path == self.backup:
            raise OSError("injected: backup remove")
        self.real_rm(store, path)

    def _finish(self, *args: Any, **kwargs: Any) -> None:
        if self._on("db_update"):
            raise tr.StaleOperation("injected: DB update")
        self.real_finish(*args, **kwargs)

    def lift(self) -> None:
        self.active = False


def _final_holds_old_or_new(store: StorageLocation, token: str) -> bytes | None:
    final = store_bytes(store, PATH)
    if final is None:
        # Only possible after a failed publish and a failed restore: the
        # original is complete in its backup.
        assert store_bytes(store, backup_path(PATH, token)) == OLD
    else:
        assert final in (OLD, NEW), final
    return final


@pytest.mark.parametrize("fault", FAULTS)
def test_publish_with_fault_then_repair(
    ctx: AppContext,
    client: TestClient,
    clock: Clock,
    protocol: str,
    fault: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if fault in NON_REPLACING_ONLY and protocol == "memory":
        pytest.skip("this step exists only where a move does not replace")
    store = _store(ctx)
    first = _upload(client, store, OLD)
    assert client.post(f"/uploads/{first['token']}/complete").status_code == 200
    res = _upload(client, store, NEW, overwrite=True)
    token = res["token"]
    faults = Faults(monkeypatch, fault, token)

    r = client.post(f"/uploads/{token}/complete")

    # During the fault.
    final = _final_holds_old_or_new(store, token)
    assert_consistent(ctx, allow_temp=True)
    (row,) = copies(ctx)
    if r.status_code == 200:
        assert (final, row.state, row.file_hash) == (NEW, FileState.AVAILABLE, md5(NEW))
    else:
        assert r.status_code == 409, r.text
        assert row.state in (FileState.AVAILABLE, FileState.OVERWRITING)
        if row.state == FileState.AVAILABLE:
            assert row.file_hash == md5(final or b"")
    expected_published = fault in (None, "backup_rm")
    assert (r.status_code == 200) == expected_published, (fault, r.text)

    # Lift the fault, let the reservation expire and repair it.
    faults.lift()
    clock.now += timedelta(hours=2)
    outcome = repair_expired(row.copy_id)
    if row.token is None:
        assert outcome == RepairResult.NOT_OWNED_OR_LIVE
    else:
        assert outcome == RepairResult.REPAIRED

    final = _final_holds_old_or_new(store, token)
    (row,) = copies(ctx)
    assert (row.state, row.token) == (FileState.AVAILABLE, None)
    assert row.file_hash == md5(final or b"")
    if fault == "backup_rm":
        # Published; only the backup is left, holding the original. Its token
        # is gone, so the index's orphan sweep removes it.
        assert temp_files(store) == [backup_path(PATH, token)]
        assert store_bytes(store, backup_path(PATH, token)) == OLD
        assert_consistent(ctx, allow_temp=True)
    else:
        assert_consistent(ctx)
    # Once the new content is at the final path it is kept (published by the
    # repair if the DB update did not happen); before that the original is.
    assert final == AFTER_REPAIR[fault]


def test_repair_restores_a_backup_when_the_final_object_is_missing(
    ctx: AppContext, client: TestClient, clock: Clock, protocol: str
) -> None:
    """A crash between "move the original to the backup" and the publish move."""
    store = _store(ctx)
    first = _upload(client, store, OLD)
    client.post(f"/uploads/{first['token']}/complete")
    res = _upload(client, store, NEW, overwrite=True)
    store.fs.mv(store.full_path(PATH), store.full_path(backup_path(PATH, res["token"])))
    clock.now += timedelta(hours=2)
    assert repair_expired(copies(ctx)[0].copy_id) == RepairResult.REPAIRED
    assert store_bytes(store, PATH) == OLD
    assert copies(ctx)[0].state == FileState.AVAILABLE
    assert_consistent(ctx)


def test_repair_publishes_staged_content_when_final_is_missing(
    ctx: AppContext, client: TestClient, clock: Clock, protocol: str
) -> None:
    """The original is gone (no backup) but the staged new content is complete."""
    store = _store(ctx)
    first = _upload(client, store, OLD)
    client.post(f"/uploads/{first['token']}/complete")
    _upload(client, store, NEW, overwrite=True)
    store.rm(PATH)
    clock.now += timedelta(hours=2)
    assert repair_expired(copies(ctx)[0].copy_id) == RepairResult.REPAIRED
    assert store_bytes(store, PATH) == NEW
    (row,) = copies(ctx)
    assert (row.state, row.file_hash) == (FileState.AVAILABLE, md5(NEW))
    assert_consistent(ctx)


def test_repair_with_nothing_usable_marks_the_copy_unknown(
    ctx: AppContext, client: TestClient, clock: Clock, protocol: str
) -> None:
    store = _store(ctx)
    first = _upload(client, store, OLD)
    client.post(f"/uploads/{first['token']}/complete")
    res = _upload(client, store, NEW, overwrite=True)
    _put(store, PATH, b"neither")
    _put(store, res["write_path"], b"partial")
    clock.now += timedelta(hours=2)
    assert repair_expired(copies(ctx)[0].copy_id) == RepairResult.REPAIRED
    (row,) = copies(ctx)
    assert (row.state, row.file_hash) == (FileState.UNKNOWN, md5(OLD))
    assert temp_files(store) == []
    assert_consistent(ctx)


def test_repair_store_error_is_incomplete_and_retried(
    ctx: AppContext,
    client: TestClient,
    clock: Clock,
    protocol: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(ctx)
    first = _upload(client, store, OLD)
    client.post(f"/uploads/{first['token']}/complete")
    _upload(client, store, NEW, overwrite=True)
    clock.now += timedelta(hours=2)
    copy_id = copies(ctx)[0].copy_id

    def broken(store: StorageLocation, path: str) -> Any:
        raise OSError("store down")

    patch_upload(monkeypatch, "observe", broken)
    assert repair_expired(copy_id) == RepairResult.INCOMPLETE  # never raises
    # Abandoned at once, so the next repair can retry without waiting a lease.
    monkeypatch.undo()
    monkeypatch.setattr(tr, "clock", clock)
    assert repair_expired(copy_id) == RepairResult.REPAIRED
    assert store_bytes(store, PATH) == OLD
    assert_consistent(ctx)

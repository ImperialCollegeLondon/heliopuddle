"""The delete_file task and delete_files flow (SVC-07)."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import pytest

from heliopuddle.config import load_projects
from heliopuddle.server import models
from heliopuddle.server.context import AppContext
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services.delete import delete_file, delete_files
from heliopuddle.server.services.verify import verify_file
from heliopuddle.storage import StorageLocation

pytestmark = pytest.mark.usefixtures("inline_prefect")


DATA = b"data"


def _add(
    ctx: AppContext, store: Path | None, path: str, location: str = "primary"
) -> models.RegisterResult:
    """Register ``path``; with a store, write it and verify it (so hp has observed it)."""
    if store is not None:
        target = store / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(DATA)
    with ctx.write_session() as session:
        result = models.register_file(
            session,
            project="demo",
            storage_location=location,
            path=path,
            hash=hashlib.md5(DATA).hexdigest(),
            size=len(DATA),
        )
    if store is not None:
        assert verify_file.fn(result.copy_id) == FileState.AVAILABLE
    return result


def _file(ctx: AppContext, file_id: int):
    with ctx.sessionmaker() as session:
        f = models.get_file(session, file_id)
        assert f is not None
        return models.to_file_read(f)


@pytest.mark.req("SVC-07")
def test_delete_file_removes_copies_and_recomputes_latest(ctx: AppContext, store: Path) -> None:
    v1 = _add(ctx, store, "a/x_v1.dat").file_id
    v2 = _add(ctx, store, "a/x_v2.dat").file_id
    # A copy in the second storage location whose object is already gone.
    _add(ctx, None, "a/x_v2.dat", location="secondary")

    assert delete_file.fn(v2, "erin") == FileState.DELETED

    rec = _file(ctx, v2)
    assert rec.deleted_at is not None
    assert rec.updated_by == "erin"
    assert not rec.is_latest
    assert [c.state for c in rec.storage_locations] == [FileState.DELETED, FileState.DELETED]
    assert not (store / "a/x_v2.dat").exists()
    assert (store / "a/x_v1.dat").exists()
    assert _file(ctx, v1).is_latest

    with pytest.raises(models.NotFoundError):
        delete_file.fn(v2)
    with pytest.raises(models.NotFoundError):
        delete_file.fn(999)


@pytest.mark.req("SVC-07")
def test_store_failure_leaves_copy_deleting_and_rerun_finishes(
    ctx: AppContext, store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_id = _add(ctx, store, "b/y.dat").file_id
    real_rm = StorageLocation.rm

    def broken(self: StorageLocation, path: str) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(StorageLocation, "rm", broken)
    assert delete_file.fn(file_id) == FileState.DELETING
    rec = _file(ctx, file_id)
    assert rec.deleted_at is None
    # A file whose only copy is deleting is not eligible to be latest (API-12).
    assert not rec.is_latest
    assert rec.state == FileState.DELETING
    assert (store / "b/y.dat").exists()

    monkeypatch.setattr(StorageLocation, "rm", real_rm)
    assert delete_file.fn(file_id) == FileState.DELETED
    assert not (store / "b/y.dat").exists()


@pytest.mark.req("SVC-07")
def test_storage_location_not_in_config_leaves_copy_deleting(
    ctx: AppContext, config_dir: Path, store: Path
) -> None:
    file_id = _add(ctx, store.parent / "store2", "c/z.dat", location="secondary").file_id
    (config_dir / "demo.yaml").write_text(
        f"project: demo\nstorage_locations:\n  - {{name: primary, url: 'file://{store}'}}\n"
    )
    ctx.projects = load_projects(config_dir)
    assert delete_file.fn(file_id) == FileState.DELETING
    assert _file(ctx, file_id).state == FileState.DELETING


@pytest.mark.req("SVC-07")
def test_delete_files_flow(ctx: AppContext, store: Path) -> None:
    ids = [_add(ctx, store, f"d/f{i}.dat").file_id for i in range(3)]
    assert delete_files.fn(ids, user="flo") == dict.fromkeys(ids, FileState.DELETED)
    assert all(_file(ctx, i).updated_by == "flo" for i in ids)
    assert not any((store / f"d/f{i}.dat").exists() for i in range(3))


@pytest.mark.req("SVC-07")
def test_delete_files_flow_fails_naming_failed_ids(
    ctx: AppContext, store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ok = _add(ctx, store, "e/ok.dat").file_id
    stuck = _add(ctx, store, "e/stuck.dat").file_id
    real_rm = StorageLocation.rm

    def selective(self: StorageLocation, path: str) -> None:
        if path == "e/stuck.dat":
            raise OSError("busy")
        real_rm(self, path)

    monkeypatch.setattr(StorageLocation, "rm", selective)
    with pytest.raises(RuntimeError, match=f"delete_file failed for: 999, {stuck}$"):
        delete_files.fn([999, ok, stuck])
    assert _file(ctx, ok).deleted_at is not None
    assert _file(ctx, stuck).state == FileState.DELETING


@pytest.mark.req("SVC-30")
def test_delete_logs_each_store_delete_and_the_file(
    ctx: AppContext, store: Path, caplog: pytest.LogCaptureFixture
) -> None:
    file_id = _add(ctx, store, "log/x_v1.dat").file_id

    with caplog.at_level(logging.INFO):
        assert delete_file.fn(file_id, "erin") == FileState.DELETED

    lines = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("deleted log/x_v1.dat from storage location primary" in m for m in lines), lines
    assert any(f"file {file_id}" in m and "demo/log/x_v1.dat" in m for m in lines), lines

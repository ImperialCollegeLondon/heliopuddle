"""Services over a storage location that holds one project folder (``path_prefix``, STO-23).

``primary`` holds the whole project; ``mirror`` has its root at the
equivalent of ``upload_mirror``, and a rule copies ``upload_mirror/**`` there.
Only files below ``upload_mirror/`` line up across the two.
"""

from __future__ import annotations

import hashlib
import io
import os
import time
from collections.abc import Iterator
from contextlib import nullcontext
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from heliopuddle.config.models import RuleSpec
from heliopuddle.server import models
from heliopuddle.server.api import create_api
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import File, FileStorageLocation
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services import rules as rule_service
from heliopuddle.server.services import transfer
from heliopuddle.server.services.index import index_full
from heliopuddle.server.services.stores import store_for
from tests.unit._rclone_helpers import (  # noqa: F401 - fixtures
    RemoteFactory,
    data_dir,
    fake_rclone,
    make_remote,
    short_tmp,
)

pytestmark = [pytest.mark.req("STO-23"), pytest.mark.usefixtures("inline_prefect")]

YAML = """\
project: demo
storage_locations:
  - name: primary
    url: file://{store}
    storage_options:
      auto_mkdir: true
  - name: mirror
    url: file://{mirror}
    path_prefix: upload_mirror
rules:
  - name: to-mirror
    action: copy
    source: primary
    destination: mirror
    match:
      paths: ["upload_mirror/**"]
"""


@pytest.fixture
def mirror(tmp_path: Path) -> Path:
    path = tmp_path / "mirror"
    path.mkdir()
    return path


@pytest.fixture
def config_dir(tmp_path: Path, store: Path, mirror: Path) -> Path:
    path = tmp_path / "config"
    path.mkdir()
    (path / "demo.yaml").write_text(YAML.format(store=store, mirror=mirror))
    return path


@pytest.fixture(autouse=True)
def _no_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transfer, "hp_concurrency", lambda *a, **k: nullcontext())
    monkeypatch.setattr(transfer, "ensure_concurrency_limit", lambda *_a: None)


def _add(ctx: AppContext, location: str, path: str, data: bytes) -> int:
    store_for(ctx, "demo", location).write(path, io.BytesIO(data))
    with ctx.write_session() as session:
        result = models.register_file(
            session,
            project="demo",
            storage_location=location,
            path=path,
            hash=hashlib.md5(data).hexdigest(),
            size=len(data),
        )
        models.set_copy_verified(
            session, result.copy_id, size=len(data), mtime=None, hash=hashlib.md5(data).hexdigest()
        )
    return result.file_id


def _copies(ctx: AppContext, location: str) -> dict[str, str]:
    with ctx.sessionmaker() as session:
        rows = session.execute(
            select(File.path, FileStorageLocation.state)
            .join(FileStorageLocation, FileStorageLocation.file_id == File.id)
            .join(FileStorageLocation.storage_location)
            .where(FileStorageLocation.storage_location.has(name=location))
        ).all()
    return {path: str(state) for path, state in rows}


def test_rule_copies_only_the_folder_and_maps_it_to_the_root(
    ctx: AppContext, store: Path, mirror: Path
) -> None:
    _add(ctx, "primary", "upload_mirror/a/x.csv", b"mirrored")
    _add(ctx, "primary", "upload_mirror/y.csv", b"also")
    _add(ctx, "primary", "elsewhere/z.csv", b"not mirrored")

    outcomes = rule_service.run_rule.fn("demo", "to-mirror", mode="full").outcomes
    assert outcomes == {"copied": 2}
    assert (mirror / "a" / "x.csv").read_bytes() == b"mirrored"
    assert (mirror / "y.csv").read_bytes() == b"also"
    assert not (mirror / "upload_mirror").exists()
    assert _copies(ctx, "mirror") == {
        "upload_mirror/a/x.csv": FileState.AVAILABLE,
        "upload_mirror/y.csv": FileState.AVAILABLE,
    }
    # A rerun finds the two copies in place and never touches the uncovered file.
    assert rule_service.run_rule.fn("demo", "to-mirror", mode="full").outcomes == {"unchanged": 2}


def test_a_one_off_rule_to_everything_only_moves_covered_files(ctx: AppContext) -> None:
    _add(ctx, "primary", "upload_mirror/x.csv", b"in")
    _add(ctx, "primary", "outside.csv", b"out")
    spec = RuleSpec.model_validate(
        {"action": "copy", "source": "primary", "destination": "mirror", "match": {"paths": ["**"]}}
    )
    assert rule_service.run_rule_once.fn("demo", spec).outcomes == {"copied": 1}
    assert set(_copies(ctx, "mirror")) == {"upload_mirror/x.csv"}


def test_index_adopts_files_under_the_project_folder(ctx: AppContext, mirror: Path) -> None:
    (mirror / "sub").mkdir()
    found = mirror / "sub" / "found.csv"
    found.write_bytes(b"found")
    settled = time.time() - 2 * 3600  # the index adopts settled files only
    os.utime(found, (settled, settled))
    index_full.fn("demo", "mirror")
    assert _copies(ctx, "mirror") == {"upload_mirror/sub/found.csv": FileState.AVAILABLE}


def test_changing_the_prefix_moves_the_copies(ctx: AppContext, config_dir: Path) -> None:
    _add(ctx, "mirror", "upload_mirror/x.csv", b"data")
    yaml = (
        (config_dir / "demo.yaml")
        .read_text()
        .replace("path_prefix: upload_mirror", "path_prefix: other_mirror")
    )
    (config_dir / "demo.yaml").write_text(yaml)
    moved = AppContext.from_settings(ctx.settings)
    try:
        moved.sync_config()
        assert _copies(moved, "mirror") == {"upload_mirror/x.csv": FileState.PENDING}
        with moved.sessionmaker() as session:
            row = models.get_storage_location(session, "demo", "mirror")
            assert row is not None and row.path_prefix == "other_mirror"
    finally:
        moved.dispose()


@pytest.fixture
def api(ctx: AppContext) -> Iterator[TestClient]:
    with TestClient(create_api(ctx)) as client:
        yield client


def test_upload_or_register_outside_the_prefix_is_422(api: TestClient) -> None:
    body = {"path": "elsewhere/x.csv", "hash": "0" * 32, "size": 1, "storage_location": "mirror"}
    for url in ("/projects/demo/uploads", "/projects/demo/files"):
        r = api.post(url, json=body)
        assert r.status_code == 422, r.text
        assert "upload_mirror/" in r.text


def test_api_reports_the_prefix(api: TestClient) -> None:
    rows = {r["name"]: r for r in api.get("/projects/demo/storage-locations").json()}
    assert rows["mirror"]["path_prefix"] == "upload_mirror"
    assert rows["primary"]["path_prefix"] == ""


def test_client_direct_access_maps_project_paths(api: TestClient, mirror: Path) -> None:
    from heliopuddle.client._base import ClientBase
    from heliopuddle.server.schemas import StorageLocationDetail

    detail = StorageLocationDetail.model_validate(
        api.get("/projects/demo/storage-locations/mirror").json()
    )
    from heliopuddle.storage import StorageLocation

    store = StorageLocation(
        detail.url, detail.storage_options, detail.create_root, detail.path_prefix
    )
    store.write("upload_mirror/c.csv", io.BytesIO(b"client"))
    assert (mirror / "c.csv").read_bytes() == b"client"
    assert ClientBase  # the client passes path_prefix in _content and _uploads the same way


class TestRcloneMirror:
    """The scenario end to end with an ``rclone://`` mirror (the stand-in rclone)."""

    @pytest.fixture
    def config_dir(
        self,
        tmp_path: Path,
        store: Path,
        make_remote: RemoteFactory,  # noqa: F811 - the fixture imported above
    ) -> Path:
        config = make_remote()
        path = tmp_path / "config"
        path.mkdir()
        (path / "demo.yaml").write_text(
            YAML.replace("url: file://{mirror}", "url: rclone://remote/root")
            .replace(
                "    path_prefix: upload_mirror\n",
                f"    path_prefix: upload_mirror\n    storage_options:\n"
                f"      config_file: {config}\n",
            )
            .format(store=store)
        )
        self.remote_root = data_dir(config) / "root"
        return path

    def test_rule_copies_the_folder_into_the_rclone_root(self, ctx: AppContext) -> None:
        _add(ctx, "primary", "upload_mirror/a/x.csv", b"to rclone")
        _add(ctx, "primary", "elsewhere/z.csv", b"stays")
        outcomes = rule_service.run_rule.fn("demo", "to-mirror", mode="full").outcomes
        assert outcomes == {"copied": 1}
        assert (self.remote_root / "a" / "x.csv").read_bytes() == b"to rclone"
        assert _copies(ctx, "mirror") == {"upload_mirror/a/x.csv": FileState.AVAILABLE}
        store = store_for(ctx, "demo", "mirror")
        assert [e.path for e in store.list_files()] == ["upload_mirror/a/x.csv"]
        assert store.md5("upload_mirror/a/x.csv") == hashlib.md5(b"to rclone").hexdigest()

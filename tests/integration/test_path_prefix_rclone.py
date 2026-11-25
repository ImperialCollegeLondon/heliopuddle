"""A real rclone storage location that holds one project folder (``path_prefix``, STO-23).

``storageA`` (local) holds the whole project, including ``upload_mirror/``;
``storageB`` is a WebDAV remote reached through rclone whose root is the
equivalent of ``upload_mirror``. Files are streamed from A to B the way a
rule's transfer does, and B lists, reads and verifies them by project path.
"""

from __future__ import annotations

import io
import uuid
from pathlib import Path

import pytest

from heliopuddle.storage import StorageLocation
from heliopuddle.storage.location import PathOutsideStorageLocation
from tests.integration.test_storage_conformance_rclone import (  # noqa: F401 - fixtures
    _use_rclone,
    dav_config,
    rclone_binary,
    webdav,
)

pytestmark = pytest.mark.req("STO-23", "STO-05")


def test_a_folder_of_one_location_is_the_root_of_another(
    tmp_path: Path,
    dav_config: Path,  # noqa: F811 - the fixture imported above
) -> None:
    storage_a = StorageLocation(f"file://{tmp_path / 'a'}", create_root=True)
    storage_a.ensure_root()
    root = f"hp-{uuid.uuid4().hex[:12]}/mirror root"
    storage_b = StorageLocation(
        f"rclone://dav/{root}",
        {"config_file": str(dav_config)},
        create_root=True,
        path_prefix="upload_mirror",
    )
    storage_b.ensure_root()
    files = {
        "upload_mirror/x.csv": b"x",
        "upload_mirror/sub dir/y.csv": b"yy",
        "other/z.csv": b"zzz",
    }
    for path, data in files.items():
        storage_a.write(path, io.BytesIO(data))

    for entry in storage_a.list_files():
        if not storage_b.covers(entry.path):
            continue
        with storage_a.open_read(entry.path) as source:
            storage_b.write(entry.path, source)

    listed = sorted(e.path for e in storage_b.list_files())
    assert listed == ["upload_mirror/sub dir/y.csv", "upload_mirror/x.csv"]
    assert storage_b.fs.exists(f"dav/{root}/x.csv")
    assert storage_b.fs.exists(f"dav/{root}/sub dir/y.csv")
    assert not storage_b.fs.exists(f"dav/{root}/upload_mirror")
    for path in listed:
        assert storage_b.md5(path) == storage_a.md5(path)
    assert sorted(e.path for e in storage_b.list_files("upload_mirror/sub dir")) == [
        "upload_mirror/sub dir/y.csv"
    ]
    assert list(storage_b.list_files("other")) == []
    with pytest.raises(PathOutsideStorageLocation):
        storage_b.write("other/z.csv", io.BytesIO(b"no"))

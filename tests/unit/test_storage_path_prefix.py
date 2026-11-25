"""A storage location that holds only a project folder (``path_prefix``, STO-23).

``storageB`` with ``path_prefix: upload_mirror`` stores the project path
``upload_mirror/x.csv`` at ``<root>/x.csv``; every other project path is
outside it. Paths given to and returned by :class:`StorageLocation` are
always project paths.
"""

from __future__ import annotations

import io
import uuid
from pathlib import Path

import pytest

from heliopuddle.storage import StorageLocation
from heliopuddle.storage.location import (
    PathOutsideStorageLocation,
    normalise_path_prefix,
    staging_path,
)

pytestmark = pytest.mark.req("STO-23")


@pytest.fixture(params=["file", "memory"])
def prefixed(request: pytest.FixtureRequest, tmp_path: Path) -> StorageLocation:
    url = (
        f"file://{tmp_path / 'b'}"
        if request.param == "file"
        else f"memory:///hp-prefix-{uuid.uuid4().hex}"
    )
    loc = StorageLocation(url, create_root=True, path_prefix="upload_mirror")
    loc.ensure_root()
    return loc


@pytest.mark.parametrize(
    ("raw", "normalised"),
    [
        ("", ""),
        ("upload_mirror", "upload_mirror"),
        ("upload_mirror/", "upload_mirror"),
        ("data/mirror", "data/mirror"),
    ],
)
def test_prefix_is_normalised(raw: str, normalised: str) -> None:
    assert normalise_path_prefix(raw) == normalised


@pytest.mark.parametrize("bad", ["/abs", "a//b", "a/../b", "./a", "..", "a/*", "a\\b", "x?"])
def test_bad_prefixes_are_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        normalise_path_prefix(bad)
    with pytest.raises(ValueError):
        StorageLocation("memory:///x", path_prefix=bad)


def test_project_paths_map_below_the_root(prefixed: StorageLocation) -> None:
    assert prefixed.full_path("upload_mirror/x.csv") == f"{prefixed.root}/x.csv"
    assert prefixed.full_path("upload_mirror/a/b.csv") == f"{prefixed.root}/a/b.csv"
    assert prefixed.covers("upload_mirror/x.csv")
    for outside in ("x.csv", "upload_mirror", "upload_mirror_old/x.csv", "other/upload_mirror/x"):
        assert not prefixed.covers(outside)
        with pytest.raises(PathOutsideStorageLocation):
            prefixed.full_path(outside)
    assert issubclass(PathOutsideStorageLocation, ValueError)


def test_io_uses_project_paths(prefixed: StorageLocation) -> None:
    prefixed.write("upload_mirror/a/x.csv", io.BytesIO(b"data"))
    assert prefixed.fs.exists(f"{prefixed.root}/a/x.csv")
    assert prefixed.exists("upload_mirror/a/x.csv")
    assert prefixed.info("upload_mirror/a/x.csv").size == 4
    with prefixed.open_read("upload_mirror/a/x.csv") as f:
        assert f.read() == b"data"
    staged = prefixed.write_staged("upload_mirror/a/y.csv", "tok", io.BytesIO(b"new"))
    assert staged == staging_path("upload_mirror/a/y.csv", "tok")
    prefixed.move(staged, "upload_mirror/a/y.csv")
    assert prefixed.md5("upload_mirror/a/y.csv")
    prefixed.rm("upload_mirror/a/x.csv")
    assert not prefixed.exists("upload_mirror/a/x.csv")
    with pytest.raises(PathOutsideStorageLocation):
        prefixed.write("elsewhere.csv", io.BytesIO(b"no"))


def test_listing_returns_project_paths(prefixed: StorageLocation) -> None:
    for path in ("upload_mirror/x.csv", "upload_mirror/a/y.csv", "upload_mirror/a/b/z.csv"):
        prefixed.write(path, io.BytesIO(b"1"))
    everything = ["upload_mirror/a/b/z.csv", "upload_mirror/a/y.csv", "upload_mirror/x.csv"]
    for scope in ("", "upload_mirror", "upload_mirror/"):
        assert sorted(e.path for e in prefixed.list_files(scope)) == everything
    assert sorted(e.path for e in prefixed.list_files("upload_mirror/a")) == everything[:2]
    assert list(prefixed.list_files("other")) == []
    assert list(prefixed.list_files("upload_mirror_old")) == []


def test_a_parent_of_a_nested_prefix_lists_everything(tmp_path: Path) -> None:
    loc = StorageLocation(f"file://{tmp_path / 'm'}", create_root=True, path_prefix="data/mirror")
    loc.ensure_root()
    loc.write("data/mirror/x.csv", io.BytesIO(b"1"))
    assert [e.path for e in loc.list_files("data")] == ["data/mirror/x.csv"]
    assert [e.path for e in loc.list_files("data/mirror")] == ["data/mirror/x.csv"]
    assert list(loc.list_files("data/other")) == []


def test_no_prefix_is_unchanged(tmp_path: Path) -> None:
    loc = StorageLocation(f"file://{tmp_path / 'a'}", create_root=True)
    loc.ensure_root()
    loc.write("upload_mirror/x.csv", io.BytesIO(b"1"))
    assert loc.full_path("x.csv") == f"{loc.root}/x.csv"
    assert [e.path for e in loc.list_files()] == ["upload_mirror/x.csv"]
    assert loc.covers("anything/at/all.csv")


def test_listing_prefixes_are_validated_and_normalised(prefixed: StorageLocation) -> None:
    prefixed.write("upload_mirror/x.csv", io.BytesIO(b"1"))
    assert [e.path for e in prefixed.list_files("./upload_mirror")] == ["upload_mirror/x.csv"]
    assert [e.path for e in prefixed.list_files("upload_mirror/a/..")] == ["upload_mirror/x.csv"]
    for bad in ("../x", "a\\b", "/abs"):
        with pytest.raises(ValueError):
            list(prefixed.list_files(bad))


def test_outside_paths_read_as_not_found(prefixed: StorageLocation) -> None:
    with pytest.raises(FileNotFoundError):
        prefixed.info("elsewhere/x.csv")
    assert not prefixed.exists("elsewhere/x.csv")
    with pytest.raises(ValueError):
        prefixed.exists("../escape")


def test_folder_paths_include_the_prefix_folder_itself(prefixed: StorageLocation) -> None:
    assert prefixed.dir_path("upload_mirror") == prefixed.root
    assert prefixed.dir_path("") == prefixed.root
    assert prefixed.dir_path("upload_mirror/a") == f"{prefixed.root}/a"
    assert prefixed.dir_path("elsewhere") is None


def test_repair_finds_staged_files_next_to_a_file_in_the_prefix_folder(
    prefixed: StorageLocation,
) -> None:
    from heliopuddle.server.services.upload.objects import temp_files

    token = str(uuid.uuid4())
    staged = prefixed.write_staged("upload_mirror/x.csv", token, io.BytesIO(b"new"))
    assert temp_files(prefixed, "upload_mirror/x.csv") == ([staged], [])
    assert temp_files(prefixed, "elsewhere/x.csv") == ([], [])

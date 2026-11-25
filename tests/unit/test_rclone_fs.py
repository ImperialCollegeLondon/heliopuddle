# ruff: noqa: F811
"""STO-05: the rclone:// fsspec filesystem, against a stand-in rcd over local dirs."""

from __future__ import annotations

import hashlib
import io
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import fsspec
import pytest

from heliopuddle.storage import StorageLocation, backends
from heliopuddle.storage.location import CreateRoot
from heliopuddle.storage.rclone import RcloneConfigError, RcloneVerifyError, manager
from heliopuddle.storage.rclone.fs import RcloneFileSystem
from tests.storage_conformance import LocationFactory, MissingRootFactory, StorageConformance
from tests.unit._rclone_helpers import (  # noqa: F401 - fixtures
    RemoteFactory,
    data_dir,
    fake_rclone,
    make_remote,
    short_tmp,
)


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _loc(config: Path, root: str = "root", **options: Any) -> StorageLocation:
    return StorageLocation(
        f"rclone://remote/{root}", {"config_file": str(config), **options}, create_root=True
    )


@pytest.mark.req("STO-07")
@pytest.mark.req("STO-05")
class TestRcloneConformance(StorageConformance):
    object_store = False

    @pytest.fixture
    def location_factory(self, make_remote: RemoteFactory) -> LocationFactory:
        def make() -> StorageLocation:
            loc = _loc(make_remote(about_free="123456"), "base dir/root")
            loc.ensure_root()
            return loc

        return make

    @pytest.fixture
    def missing_root_factory(self, make_remote: RemoteFactory) -> MissingRootFactory:
        def make(create_root: CreateRoot) -> tuple[StorageLocation, Callable[[], bool]]:
            config = make_remote()
            loc = StorageLocation(
                "rclone://remote/missing/root", {"config_file": str(config)}, create_root
            )
            return loc, lambda: (data_dir(config) / "missing" / "root").is_dir()

        return make


@pytest.mark.req("STO-05")
def test_protocol_resolves_and_urls_parse() -> None:
    assert fsspec.get_filesystem_class("rclone") is RcloneFileSystem
    strip = RcloneFileSystem._strip_protocol  # pyright: ignore[reportPrivateUsage]
    assert strip("rclone://my-box_2/a b/c.txt") == "my-box_2/a b/c.txt"
    assert strip("rclone://remote/") == "remote"
    fs = RcloneFileSystem(config_file="/nowhere/rclone.conf")
    assert fs.split("my-box_2/a b/c.txt") == ("my-box_2:", "a b/c.txt")
    assert fs.split("remote") == ("remote:", "")
    assert RcloneFileSystem._get_kwargs_from_urls("rclone://r/x") == {}  # pyright: ignore[reportPrivateUsage]
    for bad in ("", ":local:", "a:b/c", "x,y/z"):
        with pytest.raises(RcloneConfigError):
            fs.split(bad)


@pytest.mark.req("STO-05")
def test_storage_options_are_validated() -> None:
    with pytest.raises(RcloneConfigError, match="exactly one"):
        RcloneFileSystem()
    with pytest.raises(RcloneConfigError, match="verify must be"):
        RcloneFileSystem(config_file="/x", verify="maybe")


@pytest.mark.req("STO-05")
def test_only_configured_remotes_are_reachable(make_remote: RemoteFactory) -> None:
    loc = StorageLocation("rclone://other/root", {"config_file": str(make_remote())})
    with pytest.raises(RcloneConfigError, match="'other' is not defined"):
        loc.exists("x")


@pytest.mark.req("STO-05")
def test_read_with_range_and_seek(make_remote: RemoteFactory) -> None:
    loc = _loc(make_remote())
    data = bytes(range(256)) * 100
    loc.write("dir/f.bin", io.BytesIO(data))
    with loc.open_read("dir/f.bin") as f:
        f.seek(1000)
        assert f.read(10) == data[1000:1010]
        f.seek(-5, 2)
        assert f.read() == data[-5:]
    with pytest.raises(FileNotFoundError):
        loc.open_read("dir/missing.bin")
    with pytest.raises(IsADirectoryError):
        loc.fs.open(loc.full_path("dir"), "rb")
    with pytest.raises(NotImplementedError):
        loc.fs.open(loc.full_path("dir/f.bin"), "ab")


@pytest.mark.req("STO-05")
def test_ls_info_and_files(make_remote: RemoteFactory) -> None:
    loc = _loc(make_remote())
    loc.write("a/x.txt", io.BytesIO(b"xx"))
    fs, root = loc.fs, loc.root
    names = fs.ls(f"{root}/a", detail=False)
    assert names == [f"{root}/a/x.txt"]
    [entry] = fs.ls(f"{root}/a/x.txt")  # a file lists as itself
    assert entry["type"] == "file" and entry["size"] == 2
    assert isinstance(fs.modified(f"{root}/a/x.txt"), datetime)
    assert fs.size(f"{root}/a/x.txt") == 2
    assert fs.info(f"{root}/a")["type"] == "directory"
    assert fs.info(f"{root}/a")["mtime"].tzinfo == UTC
    with pytest.raises(FileNotFoundError):
        fs.ls(f"{root}/nope")


@pytest.mark.req("STO-05")
def test_dirs_copy_and_rm(make_remote: RemoteFactory) -> None:
    loc = _loc(make_remote())
    fs, root = loc.fs, loc.root
    fs.makedirs(f"{root}/d/e", exist_ok=True)
    fs.makedirs(f"{root}/d/e", exist_ok=True)
    with pytest.raises(FileExistsError):
        fs.makedirs(f"{root}/d/e")
    fs.mkdir(f"{root}/d/e")  # idempotent
    loc.write("d/e/f.txt", io.BytesIO(b"f"))
    fs.cp_file(f"{root}/d/e/f.txt", f"{root}/d/g.txt")
    assert loc.md5("d/g.txt") == _md5(b"f")
    with pytest.raises(OSError):
        fs.rmdir(f"{root}/d/e")  # not empty
    fs.rm(f"{root}/d/e", recursive=True)
    assert not loc.exists("d/e/f.txt")
    fs.rm_file(f"{root}/d/g.txt")
    fs.rmdir(f"{root}/d")
    assert not fs.exists(f"{root}/d")
    with pytest.raises(FileNotFoundError):
        fs.rm_file(f"{root}/d/g.txt")


@pytest.mark.req("STO-05")
def test_move_replaces_and_free_space(make_remote: RemoteFactory) -> None:
    loc = _loc(make_remote(about_free="4096"))
    assert loc.move_replaces
    loc.write("a.txt", io.BytesIO(b"new"))
    loc.write("b.txt", io.BytesIO(b"old"))
    loc.move("a.txt", "b.txt")
    assert loc.md5("b.txt") == _md5(b"new") and not loc.exists("a.txt")
    assert loc.free_space() == 4096
    assert loc.free_space(quota=100, indexed_size=10) == 90
    assert _loc(make_remote()).free_space() is None  # the remote has no about


@pytest.mark.req("STO-05")
def test_aborted_write_never_reaches_the_remote(make_remote: RemoteFactory) -> None:
    config = make_remote()
    loc = _loc(config)
    loc.ensure_root()
    out = backends.begin_write(loc.fs, loc.full_path("f.txt"))
    assert backends.is_deferred(out)
    out.write(b"partial" * 1000)
    spool = out.spool  # type: ignore[attr-defined]
    backends.abort_write(out)
    out.close()  # a close after the abort cannot commit
    del out
    assert not spool.exists()
    assert sorted(p.name for p in data_dir(config).rglob("*")) == ["root"]


@pytest.mark.req("STO-05")
def test_spool_is_private_and_removed(make_remote: RemoteFactory) -> None:
    loc = _loc(make_remote())
    loc.ensure_root()
    out: Any
    with loc.fs.open(loc.full_path("f.txt"), "wb") as out:
        out.write(b"data")
        spool = out.spool  # type: ignore[attr-defined]
        assert oct(spool.parent.stat().st_mode & 0o777) == "0o700"
    assert not spool.exists()
    assert loc.md5("f.txt") == _md5(b"data")


@pytest.mark.req("STO-05")
@pytest.mark.parametrize(
    ("verify", "hashes", "kind"),
    [
        ("auto", "md5", "md5"),
        ("auto", "sha1", "sha1"),
        ("native", "sha256,sha1", "sha1"),
        ("auto", "", None),
        ("content", "md5", None),
        ("size", "", "size"),
    ],
)
def test_verify_modes_record_what_was_checked(
    make_remote: RemoteFactory, verify: str, hashes: str, kind: str | None
) -> None:
    loc = _loc(make_remote(**({"hashes": hashes} if hashes else {})), verify=verify)
    loc.write("f.txt", io.BytesIO(b"payload"))
    vouched = backends.verified_md5(loc.fs, loc.full_path("f.txt"))
    assert vouched == (_md5(b"payload") if kind else None)
    assert loc.md5("f.txt") == _md5(b"payload")


@pytest.mark.req("STO-05")
def test_native_without_hashes_fails_and_removes_the_new_object(make_remote: RemoteFactory) -> None:
    config = make_remote()
    loc = _loc(config, verify="native")
    with pytest.raises(RcloneVerifyError, match="reports no hash"):
        loc.write("f.txt", io.BytesIO(b"payload"))
    assert not loc.exists("f.txt")


@pytest.mark.req("STO-05")
@pytest.mark.parametrize(
    ("verify", "corrupt", "match"),
    [
        ("native", "flip", "stored md5"),
        ("auto", "grow", "stored size"),
        ("size", "grow", "stored size"),
    ],
)
def test_mismatch_removes_a_new_destination(
    make_remote: RemoteFactory, verify: str, corrupt: str, match: str
) -> None:
    loc = _loc(make_remote(hashes="md5", corrupt=corrupt), verify=verify)
    with pytest.raises(RcloneVerifyError, match=match):
        loc.write("f.txt", io.BytesIO(b"payload"))
    assert not loc.exists("f.txt")
    assert isinstance(RcloneVerifyError("x"), OSError)


@pytest.mark.req("STO-05")
def test_mismatch_keeps_an_object_hp_did_not_create(make_remote: RemoteFactory) -> None:
    config = make_remote(hashes="sha1", corrupt="flip")
    (data_dir(config) / "root").mkdir()
    (data_dir(config) / "root" / "f.txt").write_bytes(b"first")
    loc = _loc(config)
    with pytest.raises(RcloneVerifyError, match="stored sha1"):
        loc.write("f.txt", io.BytesIO(b"other"))
    assert loc.exists("f.txt")


@pytest.mark.req("STO-05")
def test_verified_md5_follows_moves_and_changes(make_remote: RemoteFactory) -> None:
    config = make_remote(hashes="sha1")
    loc = _loc(config)
    loc.write("f.txt", io.BytesIO(b"payload"))
    loc.move("f.txt", "g.txt")
    full = loc.full_path("g.txt")
    assert backends.verified_md5(loc.fs, full) == _md5(b"payload")
    (data_dir(config) / "root" / "g.txt").write_bytes(b"changed")  # behind hp's back
    assert backends.verified_md5(loc.fs, full) is None
    assert loc.md5("g.txt") == _md5(b"changed")
    loc.rm("g.txt")
    assert backends.verified_md5(loc.fs, full) is None


@pytest.mark.req("STO-05")
def test_verified_md5_of_size_mode_checks_the_size(make_remote: RemoteFactory) -> None:
    config = make_remote()
    loc = _loc(config, verify="size")
    loc.write("f.txt", io.BytesIO(b"payload"))
    (data_dir(config) / "root" / "f.txt").write_bytes(b"longer payload")
    assert backends.verified_md5(loc.fs, loc.full_path("f.txt")) is None


@pytest.mark.req("STO-05")
def test_calls_share_one_session(make_remote: RemoteFactory) -> None:
    loc = _loc(make_remote())
    with manager.use([loc.fs.source]) as session:  # type: ignore[attr-defined]
        loc.write("a.txt", io.BytesIO(b"a"))
        assert loc.exists("a.txt")
        assert manager.session is session


def test_generic_hooks_ignore_other_filesystems() -> None:
    fs = fsspec.filesystem("memory")
    assert backends.verified_md5(fs, "/x") is None

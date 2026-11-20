"""STO-07: one storage conformance suite, run unchanged against every test backend.

A test module subclasses :class:`StorageConformance`, sets ``object_store``
and provides two fixtures:

``location_factory``
    ``() -> StorageLocation`` on a fresh, empty root that exists.
``missing_root_factory``
    ``(create_root) -> (StorageLocation, root_exists)`` on a root
    that does not exist (for an object store: a bucket or container that does
    not exist); ``root_exists()`` reports whether it has been created since.

It covers direct and staged writes (unique staging paths, STO-11), abort on a
source failure (STO-12) and both move semantics (``move_replaces``); the
missing-root case runs with each ``create_root`` value (STO-08). See
docs/design/storage.md.
"""

from __future__ import annotations

import hashlib
import io
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from heliopuddle.storage import StorageLocation, StorageRootMissingError, backends
from heliopuddle.storage.location import TEMP_SUFFIX, CreateRoot, staging_path

LocationFactory = Callable[[], StorageLocation]
MissingRootFactory = Callable[[CreateRoot], tuple[StorageLocation, Callable[[], bool]]]


MIB = 2**20


class _Boom(io.RawIOBase):
    """A source that yields ``good`` bytes, then fails: the write has started."""

    def __init__(self, good: int = 9 * MIB) -> None:
        self.left = good

    def readable(self) -> bool:
        return True

    def readinto(self, b: bytearray) -> int:  # type: ignore[override]
        if self.left <= 0:
            raise OSError("source failed mid-write")
        n = min(len(b), self.left)
        b[:n] = b"p" * n
        self.left -= n
        return n


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _raw_names(loc: StorageLocation) -> list[str]:
    """Every object under the root, as the backend lists it (including temp names)."""
    try:
        return [str(n) for n in loc.fs.find(loc.root)]
    except FileNotFoundError:
        return []


class StorageConformance:
    """The tests; subclasses provide ``location_factory`` and ``missing_root_factory``.

    Attributes:
        object_store: whether the backend is an object store, which decides
            what ``create_root: auto`` does with a missing root.
    """

    object_store: bool = False

    @pytest.fixture
    def loc(self, location_factory: LocationFactory) -> StorageLocation:
        return location_factory()

    @pytest.mark.req("STO-14")
    def test_native_free_space_and_quota(self, loc: StorageLocation) -> None:
        native = loc.free_space()
        assert native is None or native >= 0
        if self.object_store:
            assert native is None
        free = loc.free_space(quota=500, indexed_size=125)
        assert free is not None and 0 <= free <= 375
        assert loc.free_space(quota=500, indexed_size=600) == 0

    @pytest.mark.req("STO-14")
    @pytest.mark.parametrize("native", [None, 200, 1_000])
    def test_quota_uses_smaller_of_native_and_indexed_free_space(
        self, loc: StorageLocation, monkeypatch: pytest.MonkeyPatch, native: int | None
    ) -> None:
        monkeypatch.setattr(backends, "free_space", lambda *_args: native)
        expected = 375 if native is None else min(native, 375)
        assert loc.free_space(quota=500, indexed_size=125) == expected

    def test_write_from_path_read_md5_info(self, loc: StorageLocation, tmp_path: Path) -> None:
        src = tmp_path / "src.bin"
        data = b"conformance\n" * 50_000
        src.write_bytes(data)
        before = datetime.now(UTC) - timedelta(minutes=2)

        loc.write("a/b/c_v001.txt", src)

        assert loc.exists("a/b/c_v001.txt")
        with loc.open_read("a/b/c_v001.txt") as f:
            assert f.read() == data
        assert loc.md5("a/b/c_v001.txt") == hashlib.md5(data).hexdigest()
        info = loc.info("a/b/c_v001.txt")
        assert info.size == len(data)
        assert info.mtime is not None and info.mtime.tzinfo is not None
        assert before <= info.mtime <= datetime.now(UTC) + timedelta(minutes=2)

    def test_write_from_stream_and_overwrite(self, loc: StorageLocation) -> None:
        loc.write("x.bin", io.BytesIO(b"one"))
        loc.write("x.bin", io.BytesIO(b"second"))

        assert loc.md5("x.bin") == hashlib.md5(b"second").hexdigest()
        assert loc.info("x.bin").size == len(b"second")

    def test_exists_rm_and_missing_info(self, loc: StorageLocation) -> None:
        assert not loc.exists("gone.txt")
        loc.write("gone.txt", io.BytesIO(b"bye"))
        assert loc.exists("gone.txt")

        loc.rm("gone.txt")

        assert not loc.exists("gone.txt")
        with pytest.raises(FileNotFoundError):
            loc.info("gone.txt")

    def test_list_files_recursive_with_prefix(self, loc: StorageLocation) -> None:
        for path in ("a/1.txt", "a/b/2.txt", "c/3.txt", "top.txt"):
            loc.write(path, io.BytesIO(path.encode()))

        everything = {e.path: e for e in loc.list_files()}
        under_a = [e.path for e in loc.list_files("a")]

        assert sorted(everything) == ["a/1.txt", "a/b/2.txt", "c/3.txt", "top.txt"]
        assert under_a == ["a/1.txt", "a/b/2.txt"]
        assert [e.path for e in loc.list_files("a/")] == under_a
        assert [e.path for e in loc.list_files("a/b")] == ["a/b/2.txt"]
        entry = everything["a/b/2.txt"]
        assert entry.size == len(b"a/b/2.txt")
        assert entry.mtime is not None and entry.mtime.tzinfo is not None
        assert entry.md5 in {None, hashlib.md5(b"a/b/2.txt").hexdigest()}

    def test_list_files_on_empty_root_and_missing_prefix(self, loc: StorageLocation) -> None:
        assert list(loc.list_files()) == []
        loc.write("x.txt", io.BytesIO(b"x"))
        assert list(loc.list_files("no/such/prefix")) == []

    @pytest.mark.req("STO-08")
    @pytest.mark.parametrize("create_root", ["auto", True, False], ids=["auto", "true", "false"])
    def test_missing_root(
        self,
        missing_root_factory: MissingRootFactory,
        create_root: CreateRoot,
        tmp_path: Path,
    ) -> None:
        loc, root_exists = missing_root_factory(create_root)
        src = tmp_path / "f.txt"
        src.write_bytes(b"f")
        created = create_root is True or (create_root == "auto" and not self.object_store)

        if created:
            loc.write("a/f.txt", src)
            loc.write("g.txt", io.BytesIO(b"g"))

            assert loc.root_exists()
            assert root_exists()
            assert sorted(e.path for e in loc.list_files()) == ["a/f.txt", "g.txt"]
            return

        with pytest.raises(StorageRootMissingError) as excinfo:
            loc.write("a/f.txt", src)
        with pytest.raises(StorageRootMissingError):
            loc.write("g.txt", io.BytesIO(b"g"))
        with pytest.raises(StorageRootMissingError):
            loc.ensure_root()

        assert isinstance(excinfo.value, FileNotFoundError)
        assert loc.url in str(excinfo.value)
        assert not loc.root_exists()
        assert not root_exists()
        assert list(loc.list_files()) == []

    @pytest.mark.parametrize("bad", ["", "/abs", "../up", "a/../../up", "a\\b", "."])
    def test_paths_outside_root_rejected(self, loc: StorageLocation, bad: str) -> None:
        with pytest.raises(ValueError):
            loc.full_path(bad)
        with pytest.raises(ValueError):
            loc.write(bad, io.BytesIO(b"x"))

    def test_no_temp_file_left_after_success(self, loc: StorageLocation) -> None:
        loc.write("d/ok.txt", io.BytesIO(b"ok"))

        names = _raw_names(loc)
        assert names and not any(n.endswith(TEMP_SUFFIX) for n in names)
        assert not loc.exists("d/ok.txt" + TEMP_SUFFIX)

    @pytest.mark.req("STO-12")
    def test_failed_direct_write_of_a_new_path_leaves_nothing(self, loc: StorageLocation) -> None:
        loc.write("keep.txt", io.BytesIO(b"keep"))

        with pytest.raises(OSError, match="mid-write"):
            loc.write("d/f.txt", _Boom())  # type: ignore[arg-type]

        assert not loc.exists("d/f.txt")  # readers never see a partial file
        assert [e.path for e in loc.list_files()] == ["keep.txt"]
        assert not any(n.endswith(TEMP_SUFFIX) for n in _raw_names(loc))

    @pytest.mark.req("STO-12")
    @pytest.mark.req("STO-11")
    def test_failed_staged_write_keeps_the_final_and_leaves_no_staging(
        self, loc: StorageLocation
    ) -> None:
        loc.write("d/f.txt", io.BytesIO(b"original"))

        with pytest.raises(OSError, match="mid-write"):
            loc.write_staged("d/f.txt", "t1", _Boom())  # type: ignore[arg-type]

        assert loc.md5("d/f.txt") == _md5(b"original")
        assert loc.info("d/f.txt").size == len(b"original")
        assert not loc.exists(staging_path("d/f.txt", "t1"))
        assert [e.path for e in loc.list_files()] == ["d/f.txt"]

    @pytest.mark.req("STO-12")
    def test_failed_direct_write_over_an_object_keeps_it(self, loc: StorageLocation) -> None:
        if not self.object_store:
            pytest.skip("only object stores defer the commit; elsewhere overwrites are staged")
        loc.write("o.txt", io.BytesIO(b"original"))

        with pytest.raises(OSError, match="mid-write"):
            loc.write("o.txt", _Boom())  # type: ignore[arg-type]

        assert loc.md5("o.txt") == _md5(b"original")
        assert [e.path for e in loc.list_files()] == ["o.txt"]

    @pytest.mark.req("STO-11")
    def test_staged_writes_are_unique_then_moved_into_place(self, loc: StorageLocation) -> None:
        first = loc.write_staged("s/new.txt", "a", io.BytesIO(b"from a"))
        second = loc.write_staged("s/new.txt", "b", io.BytesIO(b"from b"))

        assert first != second
        assert first.endswith(TEMP_SUFFIX) and second.endswith(TEMP_SUFFIX)
        assert (loc.md5(first), loc.md5(second)) == (_md5(b"from a"), _md5(b"from b"))

        loc.move(first, "s/new.txt")  # no destination yet: works on every backend
        loc.rm(second)

        assert loc.md5("s/new.txt") == _md5(b"from a")
        assert [e.path for e in loc.list_files()] == ["s/new.txt"]

    def test_move_over_an_existing_destination_follows_move_replaces(
        self, loc: StorageLocation
    ) -> None:
        loc.write("m/final.txt", io.BytesIO(b"old"))
        staged = loc.write_staged("m/final.txt", "t", io.BytesIO(b"new"))

        if loc.move_replaces:
            loc.move(staged, "m/final.txt")
            assert loc.md5("m/final.txt") == _md5(b"new")
            assert not loc.exists(staged)
        else:
            with pytest.raises(OSError):
                loc.move(staged, "m/final.txt")
            assert loc.md5("m/final.txt") == _md5(b"old")
            assert loc.md5(staged) == _md5(b"new")

    def test_no_temp_file_left_after_failure(self, loc: StorageLocation) -> None:
        with pytest.raises(OSError, match="mid-write"):
            loc.write("d/f.txt", _Boom())  # type: ignore[arg-type]

        assert not loc.exists("d/f.txt" + TEMP_SUFFIX)
        assert not any(n.endswith(TEMP_SUFFIX) for n in _raw_names(loc))

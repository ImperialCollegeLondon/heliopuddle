"""STO-07 conformance suite on ``memory://`` and ``file://`` (STO-01)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import fsspec
import pytest
from fsspec.implementations.memory import MemoryFileSystem

from heliopuddle.storage import StorageLocation, backends
from heliopuddle.storage.location import CreateRoot
from tests.storage_conformance import LocationFactory, MissingRootFactory, StorageConformance

pytestmark = [pytest.mark.req("STO-07"), pytest.mark.req("STO-01")]

NO_REPLACE_PROTOCOL = "hpnoreplace"


class TestMemoryConformance(StorageConformance):
    @pytest.fixture
    def location_factory(self) -> LocationFactory:
        def make() -> StorageLocation:
            root = f"/hp-{uuid.uuid4().hex}"
            fsspec.filesystem("memory").mkdir(root)
            return StorageLocation(f"memory://{root}")

        return make

    @pytest.fixture
    def missing_root_factory(self) -> MissingRootFactory:
        def make(create_root: CreateRoot) -> tuple[StorageLocation, Callable[[], bool]]:
            root = f"/hp-missing-{uuid.uuid4().hex}"
            fs = fsspec.filesystem("memory")
            loc = StorageLocation(f"memory://{root}", create_root=create_root)
            return loc, lambda: bool(fs.exists(root))

        return make


class TestLocalConformance(StorageConformance):
    @pytest.fixture
    def location_factory(self, tmp_path: Path) -> LocationFactory:
        def make() -> StorageLocation:
            root = tmp_path / f"root-{uuid.uuid4().hex[:8]}"
            root.mkdir()
            return StorageLocation(root.as_uri())

        return make

    @pytest.fixture
    def missing_root_factory(self, tmp_path: Path) -> MissingRootFactory:
        def make(create_root: CreateRoot) -> tuple[StorageLocation, Callable[[], bool]]:
            root = tmp_path / f"missing-{uuid.uuid4().hex[:8]}" / "root"
            loc = StorageLocation(root.as_uri(), create_root=create_root)
            return loc, lambda: root.exists() or root.parent.exists()

        return make


class NoReplaceMemoryFileSystem(MemoryFileSystem):
    """Test-only memory filesystem whose ``mv`` refuses an existing destination (like SFTP)."""

    protocol = NO_REPLACE_PROTOCOL

    @classmethod
    def _strip_protocol(cls, path: str) -> str:  # type: ignore[override]
        return MemoryFileSystem._strip_protocol(path.removeprefix(f"{NO_REPLACE_PROTOCOL}://"))

    def mv(self, path1: str, path2: str, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        if self.exists(path2):
            raise FileExistsError(path2)
        super().mv(path1, path2, *args, **kwargs)


class TestNonReplacingConformance(StorageConformance):
    """The whole suite on a backend whose move does not replace (``move_replaces`` False)."""

    @pytest.fixture(autouse=True)
    def _register(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fsspec.register_implementation(NO_REPLACE_PROTOCOL, NoReplaceMemoryFileSystem, clobber=True)
        monkeypatch.setattr(
            backends,
            "NON_REPLACING_MOVE_PROTOCOLS",
            backends.NON_REPLACING_MOVE_PROTOCOLS | {NO_REPLACE_PROTOCOL},
        )

    @pytest.fixture
    def location_factory(self) -> LocationFactory:
        def make() -> StorageLocation:
            loc = StorageLocation(f"{NO_REPLACE_PROTOCOL}:///hp-norep-{uuid.uuid4().hex}")
            loc.ensure_root()
            assert not loc.move_replaces
            return loc

        return make

    @pytest.fixture
    def missing_root_factory(self) -> MissingRootFactory:
        def make(create_root: CreateRoot) -> tuple[StorageLocation, Callable[[], bool]]:
            root = f"/hp-norep-missing-{uuid.uuid4().hex}"
            loc = StorageLocation(f"{NO_REPLACE_PROTOCOL}://{root}", create_root=create_root)
            return loc, lambda: bool(loc.fs.exists(root))

        return make

"""STO-08: ``create_root`` (auto | true | false) on local, memory and a fake object store."""

from __future__ import annotations

import io
import shutil
import uuid
from pathlib import Path

import fsspec
import pytest

from heliopuddle.config.models import StorageLocationConfig
from heliopuddle.storage import StorageLocation, StorageRootMissingError, backends

pytestmark = pytest.mark.req("STO-08")


@pytest.fixture
def fake_object_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Treat ``memory://`` as an object store: its first path segment is the bucket."""
    monkeypatch.setattr(
        backends, "OBJECT_STORE_PROTOCOLS", backends.OBJECT_STORE_PROTOCOLS | {"memory"}
    )


def _memory_root() -> str:
    return f"/hp-{uuid.uuid4().hex[:12]}"


def test_create_root_defaults_to_auto(tmp_path: Path) -> None:
    assert StorageLocation(tmp_path.as_uri()).create_root == "auto"
    cfg = StorageLocationConfig(name="s", url=tmp_path.as_uri())
    assert cfg.create_root == "auto"


@pytest.mark.parametrize("value", ["auto", True])
def test_local_root_is_created_on_write(tmp_path: Path, value: bool | str) -> None:
    root = tmp_path / "a" / "b" / "root"
    loc = StorageLocation(root.as_uri(), create_root=value)  # type: ignore[arg-type]

    loc.write("x/f.txt", io.BytesIO(b"f"))

    assert (root / "x" / "f.txt").read_bytes() == b"f"


def test_false_is_a_clear_error_and_nothing_is_created(tmp_path: Path) -> None:
    root = tmp_path / "missing" / "root"
    loc = StorageLocation(root.as_uri(), create_root=False)

    with pytest.raises(StorageRootMissingError) as excinfo:
        loc.write("f.txt", io.BytesIO(b"f"))
    with pytest.raises(StorageRootMissingError):
        loc.ensure_root()

    assert root.as_uri() in str(excinfo.value)
    assert not root.parent.exists()


def test_ensure_root_is_a_no_op_when_the_root_exists(tmp_path: Path) -> None:
    loc = StorageLocation(tmp_path.as_uri(), create_root=False)
    loc.ensure_root()
    assert tmp_path.is_dir()


@pytest.mark.parametrize("value", ["auto", True])
def test_root_deleted_between_writes_is_recreated(tmp_path: Path, value: bool | str) -> None:
    root = tmp_path / "root"
    loc = StorageLocation(root.as_uri(), create_root=value)  # type: ignore[arg-type]
    loc.write("1.txt", io.BytesIO(b"1"))
    shutil.rmtree(root)

    loc.write("2.txt", io.BytesIO(b"2"))

    assert sorted(p.name for p in root.iterdir()) == ["2.txt"]


def test_root_deleted_between_writes_with_false_is_reported(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    loc = StorageLocation(root.as_uri(), create_root=False)
    loc.write("1.txt", io.BytesIO(b"1"))
    shutil.rmtree(root)

    with pytest.raises(StorageRootMissingError):
        loc.write("a/2.txt", io.BytesIO(b"2"))  # makedirs would recreate it: checked first
    assert not root.exists()


@pytest.mark.parametrize("value", ["auto", True])
def test_memory_root_is_created(value: bool | str) -> None:
    root = _memory_root()
    loc = StorageLocation(f"memory://{root}/sub", create_root=value)  # type: ignore[arg-type]

    loc.ensure_root()

    assert fsspec.filesystem("memory").exists(f"{root}/sub")
    loc.write("f.txt", io.BytesIO(b"f"))
    assert [e.path for e in loc.list_files()] == ["f.txt"]


@pytest.mark.usefixtures("fake_object_store")
def test_auto_does_not_create_an_object_store_bucket() -> None:
    bucket = _memory_root()
    loc = StorageLocation(f"memory://{bucket}/prefix")

    with pytest.raises(StorageRootMissingError, match="does not exist"):
        loc.ensure_root()
    with pytest.raises(StorageRootMissingError):
        loc.write("f.txt", io.BytesIO(b"f"))

    assert not fsspec.filesystem("memory").exists(bucket)


@pytest.mark.usefixtures("fake_object_store")
def test_true_creates_an_object_store_bucket() -> None:
    bucket = _memory_root()
    loc = StorageLocation(f"memory://{bucket}/prefix", create_root=True)

    loc.write("f.txt", io.BytesIO(b"f"))

    assert fsspec.filesystem("memory").exists(bucket)
    assert [e.path for e in loc.list_files()] == ["f.txt"]


@pytest.mark.usefixtures("fake_object_store")
def test_existing_bucket_with_missing_prefix_needs_no_creation() -> None:
    bucket = _memory_root()
    fsspec.filesystem("memory").mkdir(bucket)
    loc = StorageLocation(f"memory://{bucket}/no/such/prefix", create_root=False)

    loc.ensure_root()  # prefixes are implicit on an object store
    loc.write("f.txt", io.BytesIO(b"f"))


def test_creation_that_does_not_take_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    loc = StorageLocation(root.as_uri(), create_root=True)
    monkeypatch.setattr(loc.fs, "mkdir", lambda *a, **kw: None)  # a backend that ignores it

    with pytest.raises(StorageRootMissingError):
        loc.ensure_root()


def test_creation_error_is_reported_with_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loc = StorageLocation((tmp_path / "root").as_uri())

    def denied(*args: object, **kwargs: object) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(loc.fs, "mkdir", denied)

    with pytest.raises(StorageRootMissingError, match="PermissionError: denied") as excinfo:
        loc.write("f.txt", io.BytesIO(b"f"))
    assert isinstance(excinfo.value.__cause__, PermissionError)


def test_create_root_rejects_other_values() -> None:
    with pytest.raises(ValueError, match="create_root"):
        StorageLocation("memory://x", create_root="sometimes")  # type: ignore[arg-type]

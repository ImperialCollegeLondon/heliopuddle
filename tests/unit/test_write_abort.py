"""STO-12: a source failure part-way through a write never commits partial bytes.

The object-store case runs the real s3fs writer (``S3File``) with its network
calls intercepted by an in-memory fake, so the test sees exactly which S3 calls
the writer makes. Directory-style backends are covered on ``memory://`` and
``file://``. The invariants are checked over the store's state afterwards.
"""

from __future__ import annotations

import gc
import hashlib
import io
import uuid
from pathlib import Path
from typing import Any

import fsspec
import pytest

from heliopuddle.storage import StorageLocation, backends
from heliopuddle.storage.location import TEMP_SUFFIX, staging_path

pytestmark = pytest.mark.req("STO-12")

MIB = 2**20
BUCKET = "bucket"


class FailingSource(io.RawIOBase):
    """Yields ``good`` bytes, then raises: a source that fails part-way."""

    def __init__(self, good: int) -> None:
        self.left = good

    def readable(self) -> bool:
        return True

    def readinto(self, b: Any) -> int:
        if self.left <= 0:
            raise OSError("source failed mid-write")
        n = min(len(b), self.left)
        b[:n] = b"x" * n
        self.left -= n
        return n


class FakeS3:
    """Stands in for the S3 service behind a real ``S3FileSystem``.

    Records every call by its S3 operation name; objects become visible only
    on ``put_object`` or ``complete_multipart_upload``, as on S3.
    """

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.uploads: dict[str, dict[int, bytes]] = {}
        self.calls: list[str] = []
        self.fail_on: str | None = None

    def call_s3(self, method: str, *_dicts: Any, **kw: Any) -> dict[str, Any]:
        self.calls.append(method)
        if method == self.fail_on:
            raise ConnectionError(f"{method} failed")
        key = f"{kw.get('Bucket')}/{kw.get('Key')}"
        if method == "put_object":
            self.objects[key] = bytes(kw["Body"])
            return {"ETag": '"one-shot"'}
        if method == "create_multipart_upload":
            upload_id = uuid.uuid4().hex
            self.uploads[upload_id] = {}
            return {"UploadId": upload_id}
        if method == "upload_part":
            self.uploads[kw["UploadId"]][kw["PartNumber"]] = bytes(kw["Body"])
            return {"ETag": f'"part-{kw["PartNumber"]}"'}
        if method == "complete_multipart_upload":
            parts = self.uploads.pop(kw["UploadId"])
            self.objects[key] = b"".join(parts[n] for n in sorted(parts))
            return {}
        raise AssertionError(f"unexpected S3 call {method}")

    def abort_mpu(self, bucket: str, key: str, upload_id: str) -> None:
        self.calls.append("abort_multipart_upload")
        self.uploads.pop(upload_id, None)

    def exists(self, path: str, **_kw: Any) -> bool:
        path = path.strip("/")
        return path == BUCKET or path in self.objects

    def makedirs(self, path: str, exist_ok: bool = False) -> None:
        return None

    def rm(self, path: str, **_kw: Any) -> None:
        self.calls.append("delete_object")
        self.objects.pop(path.strip("/"), None)

    @property
    def writes(self) -> list[str]:
        """The calls that publish an object."""
        return [c for c in self.calls if c in {"put_object", "complete_multipart_upload"}]


@pytest.fixture
def s3() -> tuple[StorageLocation, FakeS3]:
    pytest.importorskip("s3fs")
    loc = StorageLocation(
        f"s3://{BUCKET}/root",
        {"anon": True, "skip_instance_cache": True, "default_block_size": 5 * MIB},
    )
    fake = FakeS3()
    for name in ("call_s3", "abort_mpu", "exists", "makedirs", "rm"):
        setattr(loc.fs, name, getattr(fake, name))
    return loc, fake


# -- object store: the real s3fs writer --------------------------------------


@pytest.mark.parametrize("good", [12 * MIB, 1024, 0], ids=["multipart", "small", "empty"])
def test_s3_source_failure_never_publishes_and_keeps_the_existing_object(
    s3: tuple[StorageLocation, FakeS3], good: int
) -> None:
    loc, fake = s3
    fake.objects[f"{BUCKET}/root/x.bin"] = b"old content"

    with pytest.raises(OSError, match="mid-write"):
        loc.write("x.bin", FailingSource(good))
    gc.collect()  # a finaliser must not commit the abandoned writer

    assert fake.writes == []
    assert fake.objects == {f"{BUCKET}/root/x.bin": b"old content"}
    assert fake.uploads == {}  # any multipart upload was aborted
    assert "delete_object" not in fake.calls  # the existing object is never removed
    if good >= 5 * MIB:
        assert fake.calls.count("create_multipart_upload") == 1
        assert "upload_part" in fake.calls
        assert fake.calls[-1] == "abort_multipart_upload"


def test_s3_failure_on_a_new_key_leaves_no_object(s3: tuple[StorageLocation, FakeS3]) -> None:
    loc, fake = s3

    with pytest.raises(OSError, match="mid-write"):
        loc.write("new/x.bin", FailingSource(12 * MIB))
    gc.collect()

    assert fake.objects == {}
    assert fake.uploads == {}
    assert fake.writes == []


@pytest.mark.parametrize("size", [6 * MIB + 17, 12 * MIB, 3])
def test_s3_successful_write_publishes_exactly_once(
    s3: tuple[StorageLocation, FakeS3], size: int
) -> None:
    loc, fake = s3
    data = bytes(range(256)) * (size // 256) + b"t" * (size % 256)

    loc.write("ok.bin", io.BytesIO(data))
    gc.collect()

    assert len(fake.writes) == 1
    assert hashlib.md5(fake.objects[f"{BUCKET}/root/ok.bin"]).digest() == (
        hashlib.md5(data).digest()
    )
    assert fake.uploads == {}
    assert "abort_multipart_upload" not in fake.calls


@pytest.mark.parametrize("failing", ["upload_part", "complete_multipart_upload"])
def test_s3_failure_while_finishing_aborts(
    s3: tuple[StorageLocation, FakeS3], failing: str
) -> None:
    loc, fake = s3
    fake.objects[f"{BUCKET}/root/x.bin"] = b"old content"
    fake.fail_on = failing

    with pytest.raises(ConnectionError, match=failing):
        loc.write("x.bin", io.BytesIO(b"y" * 1024))
    gc.collect()

    assert fake.objects == {f"{BUCKET}/root/x.bin": b"old content"}
    assert fake.uploads == {}
    assert "abort_multipart_upload" in fake.calls


def test_abort_write_never_raises_and_disarms_the_writer(
    s3: tuple[StorageLocation, FakeS3],
) -> None:
    loc, fake = s3
    fobj = backends.begin_write(loc.fs, f"{BUCKET}/root/z.bin")
    fobj.write(b"z" * (6 * MIB))
    fake.abort_mpu = None  # type: ignore[assignment]  # discard() itself now fails

    backends.abort_write(fobj)  # best effort: no exception
    fobj.close()
    del fobj
    gc.collect()

    assert fake.writes == []


def test_abort_write_on_a_plain_file_object_closes_it() -> None:
    closed: list[bool] = []

    class Plain(io.BytesIO):
        def close(self) -> None:
            closed.append(True)
            raise OSError("close failed")

    fobj = Plain()
    backends.abort_write(fobj)  # type: ignore[arg-type]  # the close error is swallowed
    assert closed == [True]


# -- directory-style backends: memory and local -------------------------------


@pytest.fixture(params=["memory", "local"])
def dir_loc(request: pytest.FixtureRequest, tmp_path: Path) -> StorageLocation:
    if request.param == "memory":
        root = f"/hp-abort-{uuid.uuid4().hex}"
        fsspec.filesystem("memory").mkdir(root)
        return StorageLocation(f"memory://{root}")
    root_dir = tmp_path / "root"
    root_dir.mkdir()
    return StorageLocation(root_dir.as_uri())


def _names(loc: StorageLocation) -> list[str]:
    return sorted(e.path for e in loc.list_files()) + sorted(
        str(n) for n in loc.fs.find(loc.root) if str(n).endswith(TEMP_SUFFIX)
    )


@pytest.mark.req("STO-11")
def test_failed_staged_write_leaves_the_final_object_intact(dir_loc: StorageLocation) -> None:
    dir_loc.write("d/f.bin", io.BytesIO(b"original"))
    before = (dir_loc.md5("d/f.bin"), dir_loc.info("d/f.bin").size)

    with pytest.raises(OSError, match="mid-write"):
        dir_loc.write_staged("d/f.bin", "tok1", FailingSource(3 * MIB))

    assert (dir_loc.md5("d/f.bin"), dir_loc.info("d/f.bin").size) == before
    assert not dir_loc.exists(staging_path("d/f.bin", "tok1"))
    assert _names(dir_loc) == ["d/f.bin"]


def test_failed_direct_write_of_a_new_path_leaves_nothing(dir_loc: StorageLocation) -> None:
    dir_loc.write("keep.txt", io.BytesIO(b"keep"))

    with pytest.raises(OSError, match="mid-write"):
        dir_loc.write("d/new.bin", FailingSource(3 * MIB))

    assert not dir_loc.exists("d/new.bin")
    assert _names(dir_loc) == ["keep.txt"]


def test_failure_while_closing_removes_the_new_partial(
    dir_loc: StorageLocation, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_finish(fobj: Any) -> None:
        raise OSError("close failed")

    monkeypatch.setattr(backends, "finish_write", failing_finish)
    with pytest.raises(OSError, match="close failed"):
        dir_loc.write("n.bin", io.BytesIO(b"n" * 100))

    assert not dir_loc.exists("n.bin")

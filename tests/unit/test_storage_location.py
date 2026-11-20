"""STO-01: ``StorageLocation`` details not covered by the conformance suite (STO-07)."""

from __future__ import annotations

import hashlib
import io
import shutil
from pathlib import Path

import pytest

from heliopuddle.storage import StorageLocation, StorageRootMissingError, backends

pytestmark = pytest.mark.req("STO-01")


def test_protocol_and_no_temp_writes_setting(tmp_path: Path) -> None:
    assert StorageLocation("memory://x").protocol == "memory"
    assert not hasattr(StorageLocation(tmp_path.as_uri()), "temp_writes")  # F10
    with pytest.raises(TypeError):
        StorageLocation(tmp_path.as_uri(), temp_writes=True)  # type: ignore[call-arg]


def test_md5_always_reads_even_with_a_native_md5(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loc = StorageLocation(tmp_path.as_uri())
    loc.write("n.txt", io.BytesIO(b"native"))
    assert loc.native_md5("n.txt") is None

    monkeypatch.setattr(backends, "native_md5", lambda info: "ab" * 16)

    assert loc.native_md5("n.txt") == "ab" * 16
    assert loc.md5("n.txt") == hashlib.md5(b"native").hexdigest()


def test_root_vanishing_during_a_write_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    loc = StorageLocation(root.as_uri())
    loc.write("seed.txt", io.BytesIO(b"seed"))

    def gone(*args: object, **kwargs: object) -> None:
        shutil.rmtree(root)
        raise PermissionError("backend-specific failure")

    monkeypatch.setattr(loc.fs, "makedirs", gone)
    with pytest.raises(StorageRootMissingError) as excinfo:
        loc.write("x/y.txt", io.BytesIO(b"y"))
    assert isinstance(excinfo.value.__cause__, PermissionError)


def test_write_error_kept_when_the_root_cannot_be_probed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loc = StorageLocation(tmp_path.as_uri())
    loc.write("seed.txt", io.BytesIO(b"seed"))

    def fail(*args: object, **kwargs: object) -> None:
        raise PermissionError("denied")

    calls: list[object] = []

    def probe_fails(*args: object, **kwargs: object) -> bool:
        calls.append(args)
        if len(calls) == 1:
            return True  # the check before the write
        raise ConnectionError("cannot reach the backend")

    monkeypatch.setattr(loc.fs, "open", fail)
    monkeypatch.setattr(loc.fs, "exists", probe_fails)
    with pytest.raises(PermissionError, match="denied"):
        loc.write("z.txt", io.BytesIO(b"z"))


def test_makedirs_file_exists_is_tolerated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    loc = StorageLocation(tmp_path.as_uri())

    def exists_already(*args: object, **kwargs: object) -> None:
        raise FileExistsError("prefix")

    monkeypatch.setattr(loc.fs, "makedirs", exists_already)
    (tmp_path / "p").mkdir()
    loc.write("p/f.txt", io.BytesIO(b"f"))
    assert (tmp_path / "p" / "f.txt").read_bytes() == b"f"

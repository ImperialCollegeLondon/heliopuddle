"""The client upload protocol: reserve, write, complete and failures (CLI-08)."""

from __future__ import annotations

import email.utils
import io
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from heliopuddle.client import (
    BusyError,
    ConflictError,
    HelioPuddleClient,
    HelioPuddleError,
)
from heliopuddle.storage import StorageLocation
from tests.unit import _client_upload_helpers as _fixtures
from tests.unit._client_upload_helpers import FakeServer, _local, _md5

pytestmark = pytest.mark.req("CLI-08")

hp = _fixtures.hp
server = _fixtures.server
writes = _fixtures.writes


class _FailingSource:
    def __init__(self, inner: Any, good: int) -> None:
        self.inner = inner
        self.left = good

    def read(self, size: int = -1) -> bytes:
        if self.left <= 0:
            raise OSError("local disk failed mid-read")
        chunk = self.inner.read(min(size if size > 0 else self.left, self.left))
        self.left -= len(chunk)
        return chunk


def test_new_file_reserve_then_write_then_complete(
    hp: HelioPuddleClient, server: FakeServer, writes: list[str], tmp_path: Path
) -> None:
    rec = hp.add_file(_local(tmp_path, b"payload"), "d/f.dat", metadata={"k": 1})

    assert server.log == ["project", "storage-location", "reserve", "write:d/f.dat", "complete"]
    assert server.requests[0] == {
        "path": "d/f.dat",
        "hash": _md5(b"payload"),
        "size": 7,
        "storage_location": "primary",
        "overwrite": False,
        "metadata": {"k": 1},
    }
    assert rec.hash == _md5(b"payload")
    assert server.store.md5("d/f.dat") == _md5(b"payload")
    assert server.reservations == {}


def test_unchanged_writes_nothing_and_does_not_complete(
    hp: HelioPuddleClient, server: FakeServer, writes: list[str], tmp_path: Path
) -> None:
    server.mode = "unchanged"

    rec = hp.add_file(_local(tmp_path, b"same"), "d/f.dat")

    assert rec.hash == _md5(b"same")
    assert writes == []
    assert "complete" not in server.log and "abort" not in server.log
    assert list(server.store.list_files()) == []


def test_overwrite_writes_the_staging_path_only(
    hp: HelioPuddleClient, server: FakeServer, writes: list[str], tmp_path: Path
) -> None:
    server.store.write("d/f.dat", io.BytesIO(b"old"))
    server.mode = "overwrite"
    real_complete = server._complete
    seen_before_publish: list[str] = []

    def complete(token: str) -> httpx.Response:
        seen_before_publish.append(server.store.md5("d/f.dat"))
        return real_complete(token)

    server._complete = complete  # type: ignore[method-assign]

    rec = hp.add_file(_local(tmp_path, b"new content"), "d/f.dat", overwrite=True)

    assert server.requests[0]["overwrite"] is True
    assert len(writes) == 1 and writes[0].startswith("d/f.dat.") and writes[0].endswith(".hp-tmp")
    assert seen_before_publish == [_md5(b"old")]  # the client never wrote the final path
    assert server.store.md5("d/f.dat") == _md5(b"new content")  # published by the server
    assert rec.hash == _md5(b"new content")


def test_conflict_raises_and_never_writes(
    hp: HelioPuddleClient, server: FakeServer, writes: list[str], tmp_path: Path
) -> None:
    server.store.write("d/f.dat", io.BytesIO(b"old"))
    server.mode = "conflict"

    with pytest.raises(ConflictError) as excinfo:
        hp.add_file(_local(tmp_path, b"different"), "d/f.dat")

    assert excinfo.value.status_code == 409
    assert excinfo.value.file.path == "d/f.dat"
    assert writes == []
    assert server.log[-1] == "reserve"
    assert [e.path for e in server.store.list_files()] == ["d/f.dat"]
    assert server.store.md5("d/f.dat") == _md5(b"old")


@pytest.mark.parametrize(
    ("header", "expected"),
    [("7", 7.0), ("0.5", 0.5), ("soon", None), ("-3", 0.0)],
)
def test_busy_raises_with_retry_after_and_never_writes(
    hp: HelioPuddleClient,
    server: FakeServer,
    writes: list[str],
    tmp_path: Path,
    header: str,
    expected: float | None,
) -> None:
    server.mode = "busy"
    server.retry_after = header

    with pytest.raises(BusyError) as excinfo:
        hp.add_file(_local(tmp_path, b"x"), "d/f.dat")

    assert excinfo.value.status_code == 409
    assert excinfo.value.retry_after == expected
    assert isinstance(excinfo.value, HelioPuddleError)
    assert writes == []
    assert "abort" not in server.log  # nothing was reserved
    assert list(server.store.list_files()) == []


def test_busy_retry_after_as_an_http_date(
    hp: HelioPuddleClient, server: FakeServer, tmp_path: Path
) -> None:
    server.mode = "busy"
    server.retry_after = email.utils.format_datetime(
        datetime.now(UTC) + timedelta(seconds=120), usegmt=True
    )

    with pytest.raises(BusyError) as excinfo:
        hp.add_file(_local(tmp_path, b"x"), "d/f.dat")

    assert excinfo.value.retry_after is not None
    assert 100 < excinfo.value.retry_after <= 120


@pytest.mark.req("STO-12")
@pytest.mark.parametrize("mode", ["new", "overwrite"])
def test_write_failure_aborts_the_reservation_and_publishes_nothing(
    hp: HelioPuddleClient,
    server: FakeServer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    server.store.write("keep.dat", io.BytesIO(b"keep"))
    if mode == "overwrite":
        server.store.write("d/f.dat", io.BytesIO(b"old"))
    server.mode = mode
    real = StorageLocation.write
    written: list[str] = []

    def failing_write(self: StorageLocation, path: str, source: Any) -> None:
        server.log.append("write")
        written.append(path)
        real(self, path, _FailingSource(source, 1000))

    monkeypatch.setattr(StorageLocation, "write", failing_write)

    with pytest.raises(OSError, match="mid-read"):
        hp.add_file(_local(tmp_path, b"z" * 5000), "d/f.dat", overwrite=mode == "overwrite")

    assert server.log[-3:] == ["reserve", "write", "abort"]
    assert "complete" not in server.log
    assert not server.store.exists(written[0])  # no partial object left by the client
    expected = ["d/f.dat", "keep.dat"] if mode == "overwrite" else ["keep.dat"]
    assert sorted(e.path for e in server.store.list_files()) == expected
    if mode == "overwrite":
        assert server.store.md5("d/f.dat") == _md5(b"old")


def test_complete_failure_aborts_and_reraises(
    hp: HelioPuddleClient, server: FakeServer, writes: list[str], tmp_path: Path
) -> None:
    server.complete_status = 500

    with pytest.raises(HelioPuddleError) as excinfo:
        hp.add_file(_local(tmp_path, b"c"), "d/f.dat")

    assert excinfo.value.status_code == 500
    assert server.log[-3:] == ["write:d/f.dat", "complete", "abort"]

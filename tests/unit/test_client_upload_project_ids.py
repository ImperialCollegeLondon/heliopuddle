"""Project-scoped ids in the client upload (CLI-08)."""

from __future__ import annotations

import email.utils
import io
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from heliopuddle.client import (
    HelioPuddleClient,
    NotFoundError,
)
from heliopuddle.client import _http as http_module
from heliopuddle.storage import StorageLocation
from tests.unit import _client_upload_helpers as _fixtures
from tests.unit._client_upload_helpers import API, FakeServer, _md5, _record

pytestmark = pytest.mark.req("CLI-08")

hp = _fixtures.hp
server = _fixtures.server


@pytest.fixture
def two_projects(server: FakeServer) -> None:
    server.store.write("mine.dat", io.BytesIO(b"mine"))
    server.store.write("theirs.dat", io.BytesIO(b"theirs"))
    server.files[1] = _record(1, "mine.dat", _md5(b"mine"), 4)
    server.files[2] = _record(2, "theirs.dat", _md5(b"theirs"), 6, project="other")


@pytest.fixture
def store_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    reads: list[str] = []
    real = StorageLocation.open_read

    def spy(self: StorageLocation, path: str) -> Any:
        reads.append(path)
        return real(self, path)

    monkeypatch.setattr(StorageLocation, "open_read", spy)
    return reads


@pytest.mark.usefixtures("two_projects")
def test_ids_from_another_project_are_rejected_before_storage(
    hp: HelioPuddleClient, server: FakeServer, store_reads: list[str], tmp_path: Path
) -> None:
    with pytest.raises(NotFoundError, match="not in project"):
        hp.open(2)
    with pytest.raises(NotFoundError):
        hp.download(2, tmp_path / "out.dat")
    with pytest.raises(NotFoundError):
        hp.accept_file(2)

    assert store_reads == []
    assert "storage-location" not in server.log and "accept" not in server.log
    assert not (tmp_path / "out.dat").exists()


@pytest.mark.req("CLI-08")
@pytest.mark.usefixtures("two_projects")
def test_remove_and_metadata_by_id_reject_another_project(
    hp: HelioPuddleClient, server: FakeServer
) -> None:
    with pytest.raises(NotFoundError, match="not in project"):
        hp.remove_file(2)
    with pytest.raises(NotFoundError, match="not in project"):
        hp.update_metadata(2, {"k": "v"})
    assert not any(line.startswith(("DELETE", "PATCH")) for line in server.log)


@pytest.mark.usefixtures("two_projects")
def test_ids_from_this_project_still_work(
    hp: HelioPuddleClient, server: FakeServer, store_reads: list[str], tmp_path: Path
) -> None:
    with hp.open(1) as f:
        assert f.read() == b"mine"
    assert hp.download(1, tmp_path / "out.dat").read_bytes() == b"mine"
    assert hp.accept_file(1).id == 1
    assert store_reads == ["mine.dat", "mine.dat"]


@pytest.mark.usefixtures("two_projects")
def test_ids_need_a_project(server: FakeServer) -> None:
    http = httpx.Client(base_url=API, transport=httpx.MockTransport(server.handler))
    with HelioPuddleClient(API, None, http_client=http) as hp:
        with pytest.raises(ValueError, match="project"):
            hp.open(1)
        with pytest.raises(ValueError, match="project"):
            hp.accept_file(1)
    assert server.log == []


@pytest.mark.req("CLI-08")
def test_lease_is_measured_with_the_local_clock_unless_the_server_clock_is_skewed() -> None:
    """``Date`` has one-second resolution: it is only used when the clocks disagree."""
    now = datetime.now(UTC)

    def at(when: datetime) -> httpx.Response:
        return httpx.Response(200, headers={"date": email.utils.format_datetime(when, usegmt=True)})

    local = http_module.server_now(at(now - timedelta(seconds=0.9)))
    assert abs((local - datetime.now(UTC)).total_seconds()) < 0.5
    skewed = http_module.server_now(at(now - timedelta(minutes=5)))
    assert abs((skewed - (now - timedelta(minutes=5))).total_seconds()) < 1.5
    assert http_module.server_now(httpx.Response(200)) <= datetime.now(UTC)

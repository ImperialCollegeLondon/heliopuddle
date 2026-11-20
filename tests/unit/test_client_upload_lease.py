"""The client upload lease renewal (CLI-08)."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from heliopuddle.client import (
    HelioPuddleClient,
    HelioPuddleError,
)
from heliopuddle.client import _renewer as renewer_module
from heliopuddle.storage import StorageLocation
from heliopuddle.storage import location as location_module
from tests.unit import _client_upload_helpers as _fixtures
from tests.unit._client_upload_helpers import API, FakeServer, _local, _md5

pytestmark = pytest.mark.req("CLI-08")

hp = _fixtures.hp
server = _fixtures.server


class _Slow:
    """A source that waits for renew attempts instead of sleeping for luck.

    The first read waits until the client has attempted one renew, and the end
    of the data waits for a second one, so renews always fall inside the write
    (a lost reservation is then seen on the next read).
    """

    def __init__(self, inner: Any, server: FakeServer, delay: float) -> None:
        self.inner = inner
        self.server = server
        self.delay = delay
        self.reads = 0

    def _wait_for_renews(self, count: int) -> None:
        deadline = time.monotonic() + 10
        while self.server.log.count("renew") < count and time.monotonic() < deadline:
            time.sleep(0.005)

    def read(self, size: int = -1) -> bytes:
        self.reads += 1
        if self.reads == 1:
            self._wait_for_renews(1)
        time.sleep(self.delay)
        data = self.inner.read(size)
        if not data and self.server.renew_status == 200:
            # A refused renew stops the renewer; only wait for a second one
            # when renews succeed.
            self._wait_for_renews(2)
        return data


@pytest.fixture
def slow_writes(server: FakeServer, monkeypatch: pytest.MonkeyPatch) -> None:
    """Writes read 1 KiB at a time, paced by renew attempts; renews may run 10 ms apart."""
    monkeypatch.setattr(location_module, "CHUNK_SIZE", 1024)
    monkeypatch.setattr(renewer_module.Renewer, "MIN_RENEW_INTERVAL", 0.01)
    real = StorageLocation.write

    def slow(self: StorageLocation, path: str, source: Any) -> None:
        server.log.append("write")
        real(self, path, _Slow(source, server, 0.01))

    monkeypatch.setattr(StorageLocation, "write", slow)


@pytest.mark.usefixtures("slow_writes")
def test_slow_transfer_is_renewed_before_complete(
    hp: HelioPuddleClient, server: FakeServer, tmp_path: Path
) -> None:
    server.lease = 0.3  # renew about every 0.1 s; the write takes about 0.6 s

    hp.add_file(_local(tmp_path, b"r" * 20 * 1024), "d/f.dat")

    assert server.log.count("renew") >= 2
    first, last = server.log.index("write"), server.log.index("complete")
    assert all(first < i < last for i, e in enumerate(server.log) if e == "renew")
    assert server.store.md5("d/f.dat") == _md5(b"r" * 20 * 1024)


@pytest.mark.usefixtures("slow_writes")
@pytest.mark.parametrize("status", [409, 404])
def test_lost_reservation_aborts_the_write(
    hp: HelioPuddleClient, server: FakeServer, tmp_path: Path, status: int
) -> None:
    server.lease = 0.3
    server.renew_status = status

    with pytest.raises(HelioPuddleError) as excinfo:
        hp.add_file(_local(tmp_path, b"r" * 40 * 1024), "d/f.dat")

    assert excinfo.value.status_code == status
    assert "complete" not in server.log
    assert server.log[-2:] == ["renew", "abort"]
    assert not server.store.exists("d/f.dat")  # nothing committed


@pytest.mark.usefixtures("slow_writes")
def test_renew_errors_until_the_deadline_lose_the_reservation(
    hp: HelioPuddleClient, server: FakeServer, tmp_path: Path
) -> None:
    server.lease = 0.15
    server.renew_status = 503  # transient, until the lease runs out

    with pytest.raises(HelioPuddleError) as excinfo:
        hp.add_file(_local(tmp_path, b"r" * 40 * 1024), "d/f.dat")

    assert excinfo.value.status_code == 503
    assert server.log.count("renew") >= 2
    assert "complete" not in server.log and server.log[-1] == "abort"
    assert not server.store.exists("d/f.dat")


def test_renewer_without_a_deadline_does_not_start(hp: HelioPuddleClient) -> None:
    renewer = renewer_module.Renewer(hp, "t", None, datetime.now(UTC))
    with renewer:
        assert renewer.interval is None
    renewer.raise_if_lost()


def test_renewer_survives_transport_errors_and_odd_answers(
    hp: HelioPuddleClient, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(renewer_module.Renewer, "MIN_RENEW_INTERVAL", 0.01)
    answers = iter([httpx.ConnectError("blip"), httpx.Response(200, content=b"not json")])

    def handler(request: httpx.Request) -> httpx.Response:
        answer = next(answers, None)
        if isinstance(answer, Exception):
            raise answer
        if answer is None:
            raise httpx.ConnectError("down")
        return answer

    hp._http = httpx.Client(base_url=API, transport=httpx.MockTransport(handler))
    now = datetime.now(UTC)
    with renewer_module.Renewer(hp, "t", now + timedelta(seconds=0.12), now) as renewer:
        time.sleep(0.5)
    assert renewer.renewals == 1
    with pytest.raises(HelioPuddleError, match="could not renew"):
        renewer.raise_if_lost()

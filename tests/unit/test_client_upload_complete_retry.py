"""The client upload complete step: retries and lost leases (CLI-08)."""

from __future__ import annotations

import email.utils
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from heliopuddle.client import (
    HelioPuddleClient,
    HelioPuddleError,
)
from heliopuddle.client import _renewer as renewer_module
from tests.unit import _client_upload_helpers as _fixtures
from tests.unit._client_upload_helpers import API, FakeServer, _local, _md5

pytestmark = pytest.mark.req("CLI-08")

hp = _fixtures.hp
server = _fixtures.server
writes = _fixtures.writes


@pytest.mark.req("CLI-09")
@pytest.mark.parametrize("http_date", [False, True])
def test_complete_retries_retry_after_and_keeps_renewing_during_wait(
    hp: HelioPuddleClient,
    server: FakeServer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    http_date: bool,
) -> None:
    server.lease = 0.12
    monkeypatch.setattr(renewer_module.Renewer, "MIN_RENEW_INTERVAL", 0.01)
    real = server.handler
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.url.path.endswith("/complete"):
            attempts += 1
            if attempts == 1:
                server.log.append("complete")
                retry = (
                    email.utils.format_datetime(
                        datetime.now(UTC) + timedelta(seconds=2), usegmt=True
                    )
                    if http_date
                    else "0.25"
                )
                return httpx.Response(
                    503, headers={"Retry-After": retry}, json={"detail": "store unavailable"}
                )
        return real(request)

    hp._http = httpx.Client(base_url=API, transport=httpx.MockTransport(handler))
    result = hp.add_file(_local(tmp_path, b"retry"), "d/f.dat")
    assert result.hash == _md5(b"retry")
    assert attempts == 2
    first = server.log.index("complete")
    assert "renew" in server.log[first + 1 : -1]
    assert server.log[-1] == "complete" and "abort" not in server.log


@pytest.mark.req("CLI-09")
@pytest.mark.parametrize(
    ("status", "retry_after", "expected_attempts"),
    [(503, "0", 4), (503, None, 1), (503, "bad", 1), (500, "0", 1), (409, "0", 1)],
)
def test_complete_retry_is_bounded_and_other_errors_abort_unchanged(
    hp: HelioPuddleClient,
    server: FakeServer,
    tmp_path: Path,
    status: int,
    retry_after: str | None,
    expected_attempts: int,
) -> None:
    real = server.handler

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/complete"):
            server.log.append("complete")
            return httpx.Response(
                status,
                headers={"Retry-After": retry_after} if retry_after is not None else {},
                json={"detail": "original complete failure"},
            )
        return real(request)

    hp._http = httpx.Client(base_url=API, transport=httpx.MockTransport(handler))
    with pytest.raises(HelioPuddleError, match="original complete failure") as exc:
        hp.add_file(_local(tmp_path, b"retry"), "d/f.dat")
    assert exc.value.status_code == status
    assert server.log.count("complete") == expected_attempts
    assert server.log[-1] == "abort" and not server.reservations


@pytest.mark.req("CLI-09")
def test_lost_lease_interrupts_complete_retry_wait(
    hp: HelioPuddleClient,
    server: FakeServer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server.lease = 0.12
    server.renew_status = 409
    monkeypatch.setattr(renewer_module.Renewer, "MIN_RENEW_INTERVAL", 0.01)
    real = server.handler

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/complete"):
            server.log.append("complete")
            return httpx.Response(
                503, headers={"Retry-After": "30"}, json={"detail": "store offline"}
            )
        return real(request)

    hp._http = httpx.Client(base_url=API, transport=httpx.MockTransport(handler))
    started = time.monotonic()
    with pytest.raises(HelioPuddleError, match="reservation lost"):
        hp.add_file(_local(tmp_path, b"retry"), "d/f.dat")
    assert time.monotonic() - started < 5
    assert server.log.count("complete") == 1 and server.log[-1] == "abort"


@pytest.mark.req("CLI-09")
def test_complete_claim_is_not_mistaken_for_a_lost_renewal(
    hp: HelioPuddleClient,
    server: FakeServer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server.lease = 0.12
    monkeypatch.setattr(renewer_module.Renewer, "MIN_RENEW_INTERVAL", 0.01)
    real = server.handler
    completing = False
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal completing, attempts
        if request.url.path.endswith("/renew"):
            assert not completing, "renew must not race the server's exclusive complete claim"
        if request.url.path.endswith("/complete"):
            attempts += 1
            completing = True
            time.sleep(0.15)
            completing = False
            if attempts == 1:
                server.log.append("complete")
                return httpx.Response(
                    503, headers={"Retry-After": "0.2"}, json={"detail": "store offline"}
                )
        return real(request)

    hp._http = httpx.Client(base_url=API, transport=httpx.MockTransport(handler))
    assert hp.add_file(_local(tmp_path, b"retry"), "d/f.dat").hash == _md5(b"retry")
    assert attempts == 2 and "renew" in server.log and "abort" not in server.log


def test_failed_abort_does_not_hide_the_original_error(
    hp: HelioPuddleClient, server: FakeServer, writes: list[str], tmp_path: Path
) -> None:
    server.complete_status = 500
    real = server.handler

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            raise httpx.ConnectError("server gone")
        return real(request)

    server.handler = handler  # type: ignore[method-assign]
    hp._http = httpx.Client(base_url=API, transport=httpx.MockTransport(server.handler))

    with pytest.raises(HelioPuddleError) as excinfo:
        hp.add_file(_local(tmp_path, b"c"), "d/f.dat")
    assert excinfo.value.status_code == 500

"""CLI-17 busy retries and the CLI-18 digest helper, at the client's dependency floors.

Imports only what the core install provides (see test_client_floor.py).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import httpx
import pytest

from heliopuddle.client import BusyError, HelioPuddleClient, file_digest
from heliopuddle.client import _retry as retry_module
from heliopuddle.testing import FakeHelioPuddleClient

NOW = "2026-01-02T03:04:05Z"
RECORD: dict[str, Any] = {
    "id": 7,
    "project": "demo",
    "path": "a/x.txt",
    "name": "x.txt",
    "series_id": 3,
    "size": 3,
    "hash": "1" * 32,
    "hash_algo": "md5",
    "metadata": None,
    "is_latest": True,
    "created_at": NOW,
    "updated_at": NOW,
    "storage_locations": [{"storage_location": "primary", "state": "available", "updated_at": NOW}],
}


@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Every wait between retries, instead of sleeping."""
    seen: list[float] = []
    monkeypatch.setattr(retry_module, "sleep", seen.append)
    return seen


def _client(answers: list[httpx.Response], busy_retries: int) -> tuple[HelioPuddleClient, list]:
    sent: list[httpx.Request] = []
    queue = iter(answers)

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return next(queue)

    http = httpx.Client(base_url="http://hp/api/v1", transport=httpx.MockTransport(handler))
    client = HelioPuddleClient(
        "http://hp/api/v1", "demo", user="alice", http_client=http, busy_retries=busy_retries
    )
    return client, sent


def _busy(retry_after: str) -> httpx.Response:
    return httpx.Response(
        409, json={"detail": "busy", "reason": "busy"}, headers={"Retry-After": retry_after}
    )


@pytest.mark.req("CLI-17")
def test_a_busy_call_is_sent_again_after_the_retry_after(waits: list[float]) -> None:
    client, sent = _client([_busy("4"), _busy("soon"), httpx.Response(201, json=RECORD)], 2)

    record = client.register_file("a/x.txt", "1" * 32, 3)

    assert record.id == 7
    assert len(sent) == 3
    assert sent[0].content == sent[2].content
    assert waits == [4.0, retry_module.DEFAULT_BUSY_WAIT]


@pytest.mark.req("CLI-17")
def test_busy_is_raised_once_the_retries_are_used(waits: list[float]) -> None:
    client, sent = _client([_busy("1"), _busy("2")], 1)

    with pytest.raises(BusyError) as busy:
        client.register_file("a/x.txt", "1" * 32, 3)

    assert busy.value.retry_after == 2
    assert len(sent) == 2
    assert waits == [1.0]


@pytest.mark.req("CLI-17")
def test_by_default_busy_is_raised_at_once(waits: list[float]) -> None:
    client, sent = _client([_busy("1")], 0)
    assert client.busy_retries == 0

    with pytest.raises(BusyError):
        client.register_file("a/x.txt", "1" * 32, 3)

    assert len(sent) == 1
    assert waits == []


@pytest.mark.req("CLI-17")
def test_other_errors_are_not_retried(waits: list[float]) -> None:
    client, sent = _client([httpx.Response(409, json={"detail": "different", "file": RECORD})], 3)

    with pytest.raises(Exception) as error:
        client.register_file("a/x.txt", "2" * 32, 3)

    assert type(error.value).__name__ == "ConflictError"
    assert len(sent) == 1
    assert waits == []


@pytest.mark.req("CLI-17")
def test_negative_retries_are_refused() -> None:
    with pytest.raises(ValueError, match="busy_retries"):
        HelioPuddleClient("http://hp/api/v1", "demo", busy_retries=-1)
    with pytest.raises(ValueError, match="busy_retries"):
        FakeHelioPuddleClient(busy_retries=-1)


@pytest.mark.req("CLI-14", "CLI-17")
def test_the_fake_retries_injected_busy_paths(waits: list[float], tmp_path: Path) -> None:
    fake = FakeHelioPuddleClient(busy_retries=2)
    fake.inject_busy("a/x.txt", times=2, retry_after=3.0)

    record = fake.register_file("a/x.txt", "1" * 32, 3)

    assert record.path == "a/x.txt"
    assert waits == [3.0, 3.0]
    fake.inject_busy("b/y.txt", times=3)
    with pytest.raises(BusyError):
        fake.register_file("b/y.txt", "1" * 32, 3)


@pytest.mark.req("CLI-18")
def test_file_digest_reads_md5_and_size_at_once(tmp_path: Path) -> None:
    path = tmp_path / "f.bin"
    data = b"some bytes\n" * 1000
    path.write_bytes(data)

    assert file_digest(path) == (hashlib.md5(data).hexdigest(), len(data))
    assert file_digest(str(path), chunk_size=7) == (hashlib.md5(data).hexdigest(), len(data))
    with pytest.raises(FileNotFoundError):
        file_digest(tmp_path / "missing")

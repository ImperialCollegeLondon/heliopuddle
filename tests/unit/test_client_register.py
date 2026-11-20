"""HelioPuddleClient.register_file: register content already in the store (CLI-13).

Runs against the real API in-process (header auth mode, the test default).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from heliopuddle.client import (
    ConflictError,
    HelioPuddleClient,
    HelioPuddleError,
    NotFoundError,
)
from heliopuddle.server.api import create_api
from heliopuddle.server.context import AppContext
from heliopuddle.server.schemas import FileState

API_URL = "http://testserver"


@pytest.fixture
def http(ctx: AppContext) -> Iterator[TestClient]:
    with TestClient(create_api(ctx)) as c:
        yield c


@pytest.fixture
def hp(http: TestClient) -> Iterator[HelioPuddleClient]:
    with HelioPuddleClient(API_URL, "demo", user="alice", http_client=http) as c:
        yield c


def _put(store: Path, path: str, data: bytes) -> tuple[str, int]:
    target = store / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return hashlib.md5(data).hexdigest(), len(data)


@pytest.mark.req("CLI-13")
def test_register_file_records_and_verifies_an_object_in_the_store(
    hp: HelioPuddleClient, store: Path
) -> None:
    digest, size = _put(store, "reg/a_v001.txt", b"already here")
    content_date = datetime(2026, 1, 2, tzinfo=UTC)

    record = hp.register_file(
        "reg/a_v001.txt", digest, size, metadata={"k": 1}, content_date=content_date
    )

    assert record.path == "reg/a_v001.txt"
    assert record.hash == digest
    assert record.size == size
    assert record.metadata == {"k": 1}
    assert record.state == FileState.AVAILABLE
    assert record.created_by == "alice"
    assert record.content_date is not None
    assert record.content_date.date() == content_date.date()
    assert (store / "reg/a_v001.txt").read_bytes() == b"already here"  # untouched
    assert hp.get_file_by_path("reg/a_v001.txt").id == record.id


@pytest.mark.req("CLI-13")
def test_register_file_same_hash_returns_the_existing_record(
    hp: HelioPuddleClient, store: Path
) -> None:
    digest, size = _put(store, "reg/b.txt", b"same")
    first = hp.register_file("reg/b.txt", digest, size, metadata={"v": 1})

    again = hp.register_file("reg/b.txt", digest, size, metadata={"v": 2})

    assert again.id == first.id
    assert again.metadata == {"v": 1}  # ignored for an existing record


@pytest.mark.req("CLI-13")
def test_register_file_wrong_hash_is_unknown(hp: HelioPuddleClient, store: Path) -> None:
    _, size = _put(store, "reg/c.txt", b"content")
    record = hp.register_file("reg/c.txt", "0" * 32, size)
    assert record.state == FileState.UNKNOWN


@pytest.mark.req("CLI-13")
def test_register_file_different_hash_raises_conflict(hp: HelioPuddleClient, store: Path) -> None:
    digest, size = _put(store, "reg/d.txt", b"one")
    first = hp.register_file("reg/d.txt", digest, size)
    new_digest, new_size = _put(store, "reg/d.txt", b"two!")

    with pytest.raises(ConflictError) as info:
        hp.register_file("reg/d.txt", new_digest, new_size)

    assert info.value.file.id == first.id
    assert info.value.file.hash == digest


@pytest.mark.req("CLI-13")
def test_register_file_errors(hp: HelioPuddleClient, http: TestClient, store: Path) -> None:
    digest, size = _put(store, "reg/e.txt", b"e")
    with pytest.raises(NotFoundError):
        hp.register_file("reg/e.txt", digest, size, storage_location="nowhere")
    with pytest.raises(HelioPuddleError) as info:
        hp.register_file("../escape.txt", digest, size)
    assert info.value.status_code == 422
    other = HelioPuddleClient(API_URL, "no-such-project", user="alice", http_client=http)
    with pytest.raises(NotFoundError):
        other.register_file("reg/e.txt", digest, size)

"""Verify records are reused only as the reading storage location's ``verify`` allows (STO-05).

A record made by a ``size`` write must not vouch for bytes under ``native``,
``auto`` or ``content``, and ``size`` verification must not ask the provider
for hashes (some backends read the whole object to compute them).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from heliopuddle.storage.rclone import verify as verifying
from heliopuddle.storage.rclone.verify import Written

KEY = ("rclone:file:abc", "remote:", "root/f.txt")
MD5 = hashlib.md5(b"payload").hexdigest()


class FakeClient:
    """Answers ``operations/stat`` with a fixed size and hashes, recording each call."""

    def __init__(self, size: int = 7, hashes: dict[str, str] | None = None) -> None:
        self.size = size
        self.hashes = hashes or {}
        self.calls: list[dict[str, Any]] = []

    def call(self, endpoint: str, **params: Any) -> dict[str, Any]:
        self.calls.append({"endpoint": endpoint, **params})
        assert endpoint == "operations/stat"
        shown = self.hashes if params.get("opt", {}).get("showHash") else {}
        return {"item": {"Size": self.size, "IsDir": False, "Hashes": shown}}


@pytest.fixture(autouse=True)
def _clean_record() -> Any:
    verifying.forget(KEY)
    yield
    verifying.forget(KEY)


@pytest.mark.req("STO-05")
@pytest.mark.parametrize(
    ("written_kind", "mode", "reused"),
    [
        ("size", "size", True),
        ("size", "native", False),
        ("size", "auto", False),
        ("size", "content", False),
        ("sha1", "native", True),
        ("sha1", "auto", True),
        ("sha1", "size", True),
        ("sha1", "content", False),
    ],
)
def test_records_are_reused_only_as_the_mode_allows(
    written_kind: str, mode: str, reused: bool
) -> None:
    value = "aa11" if written_kind != "size" else None
    verifying.remember(KEY, Written(7, written_kind, value, MD5))
    client = FakeClient(hashes={"sha1": "aa11"})
    found = verifying.verified_md5(client, KEY, mode)  # type: ignore[arg-type]
    assert found == (MD5 if reused else None)
    if mode == "content":
        assert client.calls == []  # content mode never asks the provider


@pytest.mark.req("STO-05")
def test_a_native_record_is_not_reused_once_the_hash_changes() -> None:
    verifying.remember(KEY, Written(7, "sha1", "aa11", MD5))
    client = FakeClient(hashes={"sha1": "bb22"})
    assert verifying.verified_md5(client, KEY, "native") is None  # type: ignore[arg-type]


@pytest.mark.req("STO-05")
def test_size_verification_asks_for_no_hashes(tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    spool.write_bytes(b"payload")
    client = FakeClient(hashes={"sha1": "aa11"})
    written = verifying.check_written(
        client,  # type: ignore[arg-type]
        "remote:",
        "root/f.txt",
        spool,
        MD5,
        7,
        "size",
    )
    assert written.kind == "size"
    (stat,) = client.calls
    assert not stat["opt"].get("showHash")

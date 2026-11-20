"""STO-11: every staged write has its own ``<path>.<token>.hp-tmp``; no cross-cleanup."""

from __future__ import annotations

import hashlib
import io
import threading
import uuid
from pathlib import Path
from typing import Any

import fsspec
import pytest

from heliopuddle.storage import StorageLocation
from heliopuddle.storage.location import TEMP_SUFFIX, staging_path

pytestmark = pytest.mark.req("STO-11")

DATA = b"identical content\n" * 200_000  # several chunks, so the two writes interleave


class SlowSource(io.RawIOBase):
    """Yields ``data`` in small reads, waiting on ``gate`` once; raises at the end if ``fail``."""

    def __init__(self, data: bytes, gate: threading.Barrier, fail: bool = False) -> None:
        self.buf = io.BytesIO(data)
        self.gate = gate
        self.fail = fail
        self.waited = False

    def readable(self) -> bool:
        return True

    def readinto(self, b: Any) -> int:
        if not self.waited:
            self.waited = True
            self.gate.wait(timeout=10)  # both writers are now mid-write
        chunk = self.buf.read(min(len(b), 64 * 1024))
        if not chunk and self.fail:
            raise OSError("source failed mid-write")
        b[: len(chunk)] = chunk
        return len(chunk)


@pytest.fixture(params=["memory", "local"])
def loc(request: pytest.FixtureRequest, tmp_path: Path) -> StorageLocation:
    if request.param == "memory":
        root = f"/hp-staging-{uuid.uuid4().hex}"
        fsspec.filesystem("memory").mkdir(root)
        return StorageLocation(f"memory://{root}")
    (tmp_path / "root").mkdir()
    return StorageLocation((tmp_path / "root").as_uri())


def _staged_names(loc: StorageLocation) -> list[str]:
    return sorted(e.path for e in loc.list_files() if e.path.endswith(TEMP_SUFFIX))


def _run_pair(loc: StorageLocation, fail_second: bool) -> dict[str, BaseException | str]:
    gate = threading.Barrier(2)
    results: dict[str, BaseException | str] = {}

    def attempt(token: str, fail: bool) -> None:
        try:
            results[token] = loc.write_staged("d/f.dat", token, SlowSource(DATA, gate, fail))
        except BaseException as e:
            results[token] = e

    threads = [
        threading.Thread(target=attempt, args=("tokA", False)),
        threading.Thread(target=attempt, args=("tokB", fail_second)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return results


def test_staging_path_is_unique_per_token() -> None:
    assert staging_path("d/f.dat", "a") != staging_path("d/f.dat", "b")
    assert staging_path("d/f.dat", "a") == "d/f.dat.a.hp-tmp"


def test_concurrent_identical_staged_writes_both_complete(loc: StorageLocation) -> None:
    loc.write("d/f.dat", io.BytesIO(b"original"))

    results = _run_pair(loc, fail_second=False)

    assert results == {"tokA": "d/f.dat.tokA.hp-tmp", "tokB": "d/f.dat.tokB.hp-tmp"}
    for staged in results.values():
        assert isinstance(staged, str)
        assert loc.md5(staged) == hashlib.md5(DATA).hexdigest()
    assert loc.md5("d/f.dat") == hashlib.md5(b"original").hexdigest()  # final untouched


def test_a_failed_attempt_cleans_up_only_its_own_staging_file(loc: StorageLocation) -> None:
    loc.write("d/f.dat", io.BytesIO(b"original"))

    results = _run_pair(loc, fail_second=True)

    assert results["tokA"] == "d/f.dat.tokA.hp-tmp"
    assert isinstance(results["tokB"], OSError)
    assert _staged_names(loc) == ["d/f.dat.tokA.hp-tmp"]
    assert loc.md5("d/f.dat.tokA.hp-tmp") == hashlib.md5(DATA).hexdigest()
    assert loc.md5("d/f.dat") == hashlib.md5(b"original").hexdigest()


def test_a_failed_attempt_leaves_other_hp_tmp_names_alone(loc: StorageLocation) -> None:
    loc.write("d/f.dat.other.hp-tmp", io.BytesIO(b"someone else's"))
    loc.write("d/f.dat.old.hp-old.hp-tmp", io.BytesIO(b"a backup"))

    with pytest.raises(OSError, match="mid-write"):
        loc.write_staged("d/f.dat", "mine", SlowSource(DATA, threading.Barrier(1), fail=True))

    assert _staged_names(loc) == ["d/f.dat.old.hp-old.hp-tmp", "d/f.dat.other.hp-tmp"]


@pytest.mark.req("STO-11")
def test_parse_temp_path_accepts_only_the_exact_shapes() -> None:
    from heliopuddle.storage.location import backup_path, parse_temp_path, staging_path

    token = "c09d35fb-afcb-4f15-b2c2-d2d41e2a1bfb"
    staged = parse_temp_path(staging_path("p/data.dat", token))
    assert staged is not None
    assert (staged.name, staged.token, staged.backup) == ("p/data.dat", token, False)
    old = parse_temp_path(backup_path("p/data.dat", token))
    assert old is not None
    assert (old.name, old.token, old.backup) == ("p/data.dat", token, True)
    for bad in (
        "p/data.dat",
        "p/data.dat.hp-tmp",
        "p/data.dat.notauuid.hp-tmp",
        f"p/data.dat.{token.upper()}.hp-tmp",
        f".{token}.hp-tmp",
        f"p/data.dat.{token.replace('-', '')}.hp-tmp",
    ):
        assert parse_temp_path(bad) is None, bad
    # a longer sibling path parses as its own, never as ``p/data.dat``'s
    sibling = parse_temp_path(staging_path("p/data.dat.v2", token))
    assert sibling is not None and sibling.name == "p/data.dat.v2"

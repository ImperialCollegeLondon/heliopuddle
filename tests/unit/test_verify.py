"""The verify_file task and verify_files flow (SVC-02)."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import pytest

from heliopuddle.server import models
from heliopuddle.server.context import AppContext
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services.verify import verify_file, verify_files

pytestmark = pytest.mark.usefixtures("inline_prefect")


def _register(ctx: AppContext, store: Path, path: str, content: bytes | None, digest: str) -> int:
    if content is not None:
        target = store / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    with ctx.write_session() as session:
        return models.register_file(
            session,
            project="demo",
            storage_location="primary",
            path=path,
            hash=digest,
            size=len(content or b""),
        ).copy_id


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _copy(ctx: AppContext, copy_id: int):
    with ctx.sessionmaker() as session:
        copy = models.get_copy(session, copy_id)
        assert copy is not None
        return copy


@pytest.mark.req("SVC-02")
def test_verify_file_match_sets_available_and_records(ctx: AppContext, store: Path) -> None:
    copy_id = _register(ctx, store, "d/a.dat", b"hello", _md5(b"hello").upper())

    assert verify_file.fn(copy_id) == FileState.AVAILABLE

    copy = _copy(ctx, copy_id)
    assert copy.state == FileState.AVAILABLE
    assert copy.size == 5
    assert copy.hash == _md5(b"hello")
    assert copy.mtime is not None
    assert copy.last_seen_at is not None


@pytest.mark.req("SVC-02")
def test_verify_file_mismatch_sets_unknown(ctx: AppContext, store: Path) -> None:
    copy_id = _register(ctx, store, "b.dat", b"hello", _md5(b"other"))
    assert verify_file.fn(copy_id) == FileState.UNKNOWN
    copy = _copy(ctx, copy_id)
    assert copy.state == FileState.UNKNOWN
    assert copy.hash is None


@pytest.mark.req("SVC-02")
def test_verify_file_missing_sets_unknown(ctx: AppContext, store: Path) -> None:
    copy_id = _register(ctx, store, "missing.dat", None, _md5(b""))
    assert verify_file.fn(copy_id) == FileState.UNKNOWN
    assert _copy(ctx, copy_id).state == FileState.UNKNOWN


@pytest.mark.req("SVC-02")
def test_verify_file_unknown_copy_raises(ctx: AppContext) -> None:
    with pytest.raises(models.NotFoundError):
        verify_file.fn(12345)


@pytest.mark.req("SVC-02")
def test_verify_file_location_not_in_config_raises(ctx: AppContext, store: Path) -> None:
    copy_id = _register(ctx, store, "c.dat", b"x", _md5(b"x"))
    ctx.projects["demo"].storage_locations.pop(0)
    with pytest.raises(LookupError, match="not in config"):
        verify_file.fn(copy_id)
    assert _copy(ctx, copy_id).state == FileState.PENDING


@pytest.mark.req("SVC-02")
def test_verify_files_flow_matches_task(ctx: AppContext, store: Path) -> None:
    good = _register(ctx, store, "g.dat", b"good", _md5(b"good"))
    bad = _register(ctx, store, "x.dat", b"bad", _md5(b"not bad"))
    gone = _register(ctx, store, "gone.dat", None, _md5(b""))

    result = verify_files.fn([good, bad, gone])

    assert result == {good: FileState.AVAILABLE, bad: FileState.UNKNOWN, gone: FileState.UNKNOWN}
    # Re-running the task on the same copies gives the same answer.
    assert {i: verify_file.fn(i) for i in (good, bad, gone)} == result


@pytest.mark.req("SVC-02")
def test_verify_files_flow_fails_when_a_task_fails(ctx: AppContext, store: Path) -> None:
    good = _register(ctx, store, "g.dat", b"good", _md5(b"good"))
    with pytest.raises(RuntimeError, match="999"):
        verify_files.fn([good, 999])
    assert _copy(ctx, good).state == FileState.AVAILABLE


@pytest.mark.req("SVC-30")
def test_verify_logs_ok_at_info_and_mismatch_at_warning(
    ctx: AppContext, store: Path, caplog: pytest.LogCaptureFixture
) -> None:
    good = _register(ctx, store, "log/a_v1.dat", b"abc", _md5(b"abc"))
    bad = _register(ctx, store, "log/b_v1.dat", b"abc", _md5(b"other"))

    with caplog.at_level(logging.INFO):
        verify_file.fn(good)
        verify_file.fn(bad)

    ok = [r for r in caplog.records if "log/a_v1.dat" in r.getMessage()]
    assert ok and ok[0].levelno == logging.INFO and "hash ok" in ok[0].getMessage()
    fail = [r for r in caplog.records if "log/b_v1.dat" in r.getMessage()]
    assert fail and fail[0].levelno == logging.WARNING and "demo" in fail[0].getMessage()

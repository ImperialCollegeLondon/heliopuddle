# ruff: noqa: F811
"""STO-20, STO-22: rclone sessions (one rcd per config set) and keeping rcd private."""

from __future__ import annotations

import logging
import os
import stat
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from heliopuddle.storage.rclone import (
    ConfigSource,
    RcloneBusyError,
    RcloneConfigError,
    RcloneUnavailable,
    configure,
    manager,
    set_session_hooks,
    wait_limit,
)
from heliopuddle.storage.rclone.client import ALLOWED_ENDPOINTS
from heliopuddle.storage.rclone.config import source_from_options
from heliopuddle.storage.rclone.process import kill_all
from tests.unit._rclone_helpers import (  # noqa: F401 - fixtures
    RemoteFactory,
    fake_rclone,
    make_remote,
    short_tmp,
)


def _file(config: Path) -> ConfigSource:
    return source_from_options({"config_file": str(config)})


class MemoryBlocks:
    """A block store holding config text in memory."""

    def __init__(self, **blocks: str) -> None:
        self.blocks = dict(blocks)
        self.saved: list[tuple[str, str]] = []

    def load(self, name: str) -> str:
        return self.blocks[name]  # KeyError is a LookupError

    def save(self, name: str, contents: str) -> None:
        self.saved.append((name, contents))
        self.blocks[name] = contents


@pytest.mark.req("STO-20")
def test_lock_names() -> None:
    assert ConfigSource("block", "box-prod").lock_name == "rclone:box-prod"
    lock = ConfigSource("file", "/etc/rclone.conf").lock_name
    assert lock.startswith("rclone:file:") and len(lock) == len("rclone:file:") + 12
    assert "/" not in lock


@pytest.mark.req("STO-20")
@pytest.mark.parametrize(
    "options", [{}, {"config_block": "a", "config_file": "/b"}, {"config_block": ""}]
)
def test_exactly_one_config_source(options: dict[str, str]) -> None:
    with pytest.raises(RcloneConfigError, match="exactly one"):
        source_from_options(options)


@pytest.mark.req("STO-20")
def test_session_is_shared_and_closes_when_idle(make_remote: RemoteFactory) -> None:
    source = _file(make_remote())
    with manager.use([source]) as first:
        with manager.use([source]) as nested:
            assert nested is first
            assert first.refs == 2
        process = first.rcd.process  # type: ignore[union-attr]
    with manager.use([source]) as again:  # within the idle time: still the same rcd
        assert again is first
    assert first.done.wait(5)
    assert process.poll() is not None
    assert first.directory is not None and not first.directory.exists()
    assert manager.session is None


@pytest.mark.req("STO-20")
def test_other_config_waits_then_opens_the_union(make_remote: RemoteFactory) -> None:
    a, b = _file(make_remote("a")), _file(make_remote("b"))
    opened: list[frozenset[ConfigSource]] = []
    with manager.use([a]) as first:
        thread = threading.Thread(target=lambda: opened.append(_use_sources([b])), name="needs-b")
        thread.start()
        time.sleep(0.1)
        assert first.draining  # no new callers join it
        assert not opened
    thread.join(10)
    assert opened == [frozenset({b})]
    with manager.use([a, b]) as both:
        assert both.remotes == {"a", "b"}


def _open(sources: list[ConfigSource], **wait: float | None) -> None:
    with manager.use(sources, **wait):
        pass


def _use_sources(sources: list[ConfigSource]) -> frozenset[ConfigSource]:
    with manager.use(sources) as session:
        return session.sources


@pytest.mark.req("STO-20")
def test_waiters_get_the_union_of_their_configs(make_remote: RemoteFactory) -> None:
    a, b, c = (_file(make_remote(n)) for n in "abc")
    seen: list[frozenset[ConfigSource]] = []
    with manager.use([a]):
        threads = [
            threading.Thread(target=lambda s=s: seen.append(_use_sources([s]))) for s in (b, c)
        ]
        for t in threads:
            t.start()
        time.sleep(0.2)
    for t in threads:
        t.join(10)
    assert seen == [frozenset({b, c})] * 2


@pytest.mark.req("STO-20")
def test_nested_use_needing_another_config_fails_fast(make_remote: RemoteFactory) -> None:
    a, b = _file(make_remote("a")), _file(make_remote("b"))
    with manager.use([a]), pytest.raises(RcloneConfigError, match="all of them first"):
        _open([b], wait=1)


@pytest.mark.req("STO-20")
def test_busy_lock_times_out(make_remote: RemoteFactory) -> None:
    source = _file(make_remote())
    with manager.lock_factory([source.lock_name], None):  # held "elsewhere"
        started = time.monotonic()
        with wait_limit(0.2), pytest.raises(RcloneBusyError, match="in use elsewhere"):
            _open([source])
        assert time.monotonic() - started < 5
    assert isinstance(RcloneBusyError("x"), TimeoutError)
    with manager.use([source]) as session:
        assert session.remotes == {"remote"}


@pytest.mark.req("STO-20")
def test_waiting_for_another_session_times_out(make_remote: RemoteFactory) -> None:
    a, b = _file(make_remote("a")), _file(make_remote("b"))
    holding = threading.Event()
    done = threading.Event()

    def hold() -> None:
        with manager.use([a]):
            holding.set()
            done.wait(10)

    thread = threading.Thread(target=hold)
    thread.start()
    assert holding.wait(10)
    try:
        with pytest.raises(RcloneBusyError, match="busy"):
            _open([b], wait=0.1)
    finally:
        done.set()
        thread.join(10)
    _open([b], wait=10)


@pytest.mark.req("STO-20")
def test_lock_factory_gets_lock_names_and_runs_in_one_thread(make_remote: RemoteFactory) -> None:
    a, b = _file(make_remote("a")), _file(make_remote("b"))
    calls: list[tuple[list[str], float | None, int, int]] = []

    @contextmanager
    def recording(names: list[str], timeout: float | None) -> Iterator[None]:
        entered = threading.get_ident()
        try:
            yield
        finally:
            calls.append((names, timeout, entered, threading.get_ident()))

    set_session_hooks(lock_factory=recording)
    with manager.use([a, b], wait=7):
        pass
    manager.close(5)
    [(names, timeout, entered, left)] = calls
    assert names == sorted([a.lock_name, b.lock_name])
    assert timeout is not None and 0 < timeout <= 7
    assert entered == left != threading.get_ident()


@pytest.mark.req("STO-20")
def test_default_local_lock_serialises_one_config(make_remote: RemoteFactory) -> None:
    source = _file(make_remote())
    lock = manager.lock_factory
    with lock([source.lock_name], None), pytest.raises(TimeoutError):
        _ = lock([source.lock_name], 0.05).__enter__()


@pytest.mark.req("STO-20")
def test_changed_config_is_written_back_even_after_a_failure(make_remote: RemoteFactory) -> None:
    config = make_remote(refresh="true")
    os.chmod(config, 0o640)
    with pytest.raises(RuntimeError, match="the caller failed"), manager.use([_file(config)]):
        raise RuntimeError("the caller failed")
    manager.close(5)
    assert "token = refreshed" in config.read_text()
    assert stat.S_IMODE(config.stat().st_mode) == 0o640


@pytest.mark.req("STO-20")
def test_unchanged_config_is_not_rewritten(make_remote: RemoteFactory) -> None:
    config = make_remote()
    config.write_text("# keep this comment\n" + config.read_text())
    with manager.use([_file(config)]):
        pass
    manager.close(5)
    assert config.read_text().startswith("# keep this comment")


@pytest.mark.req("STO-20")
def test_config_block_through_the_block_store(make_remote: RemoteFactory) -> None:
    text = make_remote("boxy", refresh="true").read_text()
    blocks = MemoryBlocks(**{"box-prod": text})
    set_session_hooks(block_store=blocks)
    with manager.use([ConfigSource("block", "box-prod")]) as session:
        assert session.remotes == {"boxy"}
    manager.close(5)
    [(name, saved)] = blocks.saved
    assert name == "box-prod" and "token = refreshed" in saved


@pytest.mark.req("STO-20")
def test_missing_block_and_missing_store(make_remote: RemoteFactory) -> None:
    with pytest.raises(RcloneUnavailable, match="no config block store"):
        _open([ConfigSource("block", "nope")])
    set_session_hooks(block_store=MemoryBlocks())
    with pytest.raises(RcloneUnavailable, match="'nope' does not exist"):
        _open([ConfigSource("block", "nope")])


@pytest.mark.req("STO-20")
def test_duplicate_remote_names_are_a_config_error(make_remote: RemoteFactory) -> None:
    a, b = _file(make_remote("same")), _file(make_remote("same"))
    with pytest.raises(RcloneConfigError, match="'same' is defined by both"):
        _open([a, b])


@pytest.mark.req("DEP-21")
def test_missing_binary_is_unavailable(make_remote: RemoteFactory, short_tmp: Path) -> None:
    config = make_remote()
    configure(binary=str(short_tmp / "no-rclone"))
    with pytest.raises(RcloneUnavailable, match="not an executable"):
        _open([_file(config)])


@pytest.mark.req("STO-22")
def test_private_files_and_command_line(make_remote: RemoteFactory) -> None:
    with manager.use([_file(make_remote())]) as session:
        directory = session.directory
        assert directory is not None
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert stat.S_IMODE((directory / "rclone.conf").stat().st_mode) == 0o600
        assert stat.S_ISSOCK((directory / "rc.sock").stat().st_mode)
        process = session.rcd.process  # type: ignore[union-attr]
        args = [str(a) for a in process.args]  # type: ignore[union-attr]
        assert args[1:3] == ["rcd", "--config"]
        assert "--rc-serve" in args and f"unix://{directory}/rc.sock" in args
        for forbidden in ("--rc-no-auth", "--rc-web-gui", "--rc-enable-metrics", "--rc-pass"):
            assert forbidden not in args
        assert os.getpgid(process.pid) == process.pid  # its own process group
    manager.close(5)
    assert not directory.exists()


@pytest.mark.req("STO-22")
def test_inherited_rclone_settings_cannot_loosen_auth(
    make_remote: RemoteFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RCLONE_RC_NO_AUTH", "true")
    monkeypatch.setenv("RCLONE_RC_USER", "")
    with manager.use([_file(make_remote())]) as session:
        assert session.client.call("core/version")["version"]


@pytest.mark.req("STO-22")
def test_credentials_never_in_repr_or_logs(
    make_remote: RemoteFactory, caplog: pytest.LogCaptureFixture
) -> None:
    config = make_remote(refresh="true", secret_key="s3cr3t-value")
    caplog.set_level(logging.DEBUG, logger="heliopuddle")
    with manager.use([_file(config)]) as session:
        env = session.rcd.process  # type: ignore[union-attr]
        password = _rc_password(env.pid)
        texts = [repr(session), repr(session.client), repr(session.rcd), str(vars(session.rcd))]
    manager.close(5)
    texts.append(caplog.text)
    assert password
    for text in texts:
        assert password not in text
        assert "s3cr3t-value" not in text


def _rc_password(pid: int) -> str:
    """The rc password from rcd's environment (readable by its owner only)."""
    try:
        environ = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        pytest.skip("no /proc")
    for entry in environ:
        if entry.startswith(b"RCLONE_RC_PASS="):
            return entry.split(b"=", 1)[1].decode()
    raise AssertionError("no rc password")


@pytest.mark.req("STO-22")
def test_kill_all_kills_rcd(make_remote: RemoteFactory) -> None:
    with manager.use([_file(make_remote())]) as session:
        process = session.rcd.process  # type: ignore[union-attr]
        kill_all()
        assert process.wait(5) is not None


@pytest.mark.req("STO-22")
def test_endpoint_allowlist() -> None:
    assert {
        *(
            f"operations/{n}"
            for n in (
                [
                    "list",
                    "stat",
                    "about",
                    "hashsum",
                    "copyfile",
                    "movefile",
                    "deletefile",
                    "mkdir",
                    "rmdir",
                    "purge",
                ]
            )
        ),
        "config/listremotes",
        "core/version",
        "core/quit",
    } == ALLOWED_ENDPOINTS


class FailingBlocks(MemoryBlocks):
    def save(self, name: str, contents: str) -> None:
        raise ConnectionError("block store down")


@pytest.mark.req("STO-20")
def test_failed_write_back_is_reported_not_raised(
    make_remote: RemoteFactory, caplog: pytest.LogCaptureFixture
) -> None:
    blocks = FailingBlocks(cfg=make_remote(refresh="true").read_text())
    set_session_hooks(block_store=blocks)
    with manager.use([ConfigSource("block", "cfg")]) as session:
        pass
    manager.close(5)
    assert session.writeback_errors == ["saving rclone config block 'cfg' failed: ConnectionError"]
    assert "saving rclone config block 'cfg' failed" in caplog.text


@pytest.mark.req("STO-22")
def test_invalid_or_missing_config_never_quotes_contents(
    make_remote: RemoteFactory, short_tmp: Path
) -> None:
    bad = short_tmp / "bad.conf"
    bad.write_text("token = s3cr3t-outside-any-section\n")
    with pytest.raises(RcloneConfigError, match="not a valid config") as info:
        _open([_file(bad)])
    assert "s3cr3t" not in str(info.value)
    twice = short_tmp / "twice.conf"
    twice.write_text("[a]\ntype = alias\n[a]\ntype = alias\n")
    with pytest.raises(RcloneConfigError, match="defines remote 'a' twice"):
        _open([_file(twice)])
    with pytest.raises(RcloneUnavailable, match="does not exist"):
        _open([_file(short_tmp / "missing.conf")])


@pytest.mark.req("STO-20")
def test_config_file_aliases_share_one_lock(tmp_path: Path) -> None:
    real = tmp_path / "conf" / "rclone.conf"
    real.parent.mkdir()
    real.write_text("[a]\ntype = memory\n")
    link = tmp_path / "link.conf"
    link.symlink_to(real)
    dotted = tmp_path / "conf" / ".." / "conf" / "rclone.conf"
    names = {source_from_options({"config_file": str(p)}).lock_name for p in (real, link, dotted)}
    assert len(names) == 1

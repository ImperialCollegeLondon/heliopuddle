# ruff: noqa: F811
"""STO-20: several rclone sessions per process, draining, and one deadline per call."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from heliopuddle.storage.rclone import (
    ConfigSource,
    RcloneBusyError,
    configure,
    manager,
    set_session_hooks,
)
from heliopuddle.storage.rclone.config import source_from_options
from heliopuddle.storage.rclone.session import Session
from tests.unit._rclone_helpers import (  # noqa: F401 - fixtures
    RemoteFactory,
    fake_rclone,
    make_remote,
    short_tmp,
)


def _file(config: Path) -> ConfigSource:
    return source_from_options({"config_file": str(config)})


def _open(sources: list[ConfigSource], **wait: float | None) -> None:
    with manager.use(sources, **wait):
        pass


def _use_sources(sources: list[ConfigSource]) -> frozenset[ConfigSource]:
    with manager.use(sources) as session:
        return session.sources


@pytest.mark.req("STO-20")
def test_two_configs_run_in_two_sessions_at_once(make_remote: RemoteFactory) -> None:
    a, b = _file(make_remote("a")), _file(make_remote("b"))
    both_inside = threading.Barrier(2, timeout=10)
    seen: dict[str, Session] = {}

    def run(name: str, source: ConfigSource) -> None:
        with manager.use([source]) as session:
            seen[name] = session
            both_inside.wait()  # raises BrokenBarrierError unless both run at once

    threads = [threading.Thread(target=run, args=(n, s)) for n, s in (("a", a), ("b", b))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert seen["a"] is not seen["b"]
    assert (seen["a"].sources, seen["b"].sources) == (frozenset({a}), frozenset({b}))
    assert (seen["a"].remotes, seen["b"].remotes) == ({"a"}, {"b"})


@pytest.mark.req("STO-20")
def test_unrelated_configs_with_one_remote_name_never_merge(make_remote: RemoteFactory) -> None:
    a = _file(make_remote("a"))
    b, c = _file(make_remote("archive")), _file(make_remote("archive"))
    both_inside = threading.Barrier(2, timeout=10)
    seen: list[frozenset[ConfigSource]] = []
    errors: list[BaseException] = []

    def run(source: ConfigSource) -> None:
        try:
            with manager.use([source]) as session:
                assert session.remotes == {"archive"}
                seen.append(session.sources)
                both_inside.wait()
        except BaseException as e:  # reported below
            errors.append(e)

    with manager.use([a]) as first:
        threads = [threading.Thread(target=run, args=(s,)) for s in (b, c)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        assert not first.draining
    assert not errors
    assert sorted(seen, key=repr) == sorted([frozenset({b}), frozenset({c})], key=repr)


@pytest.mark.req("STO-20")
def test_overlapping_request_waits_for_the_session_to_drain(make_remote: RemoteFactory) -> None:
    a, b = _file(make_remote("a")), _file(make_remote("b"))
    opened: list[frozenset[ConfigSource]] = []
    with manager.use([a]) as first:
        thread = threading.Thread(target=lambda: opened.append(_use_sources([a, b])))
        thread.start()
        time.sleep(0.1)
        assert first.draining  # no new callers join it
        assert not opened
    thread.join(10)
    assert opened == [frozenset({a, b})]
    assert first.done.is_set()


class GatedLocks:
    """A lock factory whose grant waits for :attr:`gate` (ignoring the timeout)."""

    def __init__(self) -> None:
        self.gate = threading.Event()
        self.entered: list[list[str]] = []
        self.left: list[list[str]] = []

    @contextmanager
    def __call__(self, names: list[str], timeout: float | None) -> Iterator[None]:
        assert self.gate.wait(10)
        self.entered.append(names)
        try:
            yield
        finally:
            self.left.append(names)


@pytest.mark.req("STO-20")
def test_a_starting_session_past_the_deadline_drops_only_this_caller(
    make_remote: RemoteFactory,
) -> None:
    source = _file(make_remote())
    locks = GatedLocks()
    set_session_hooks(lock_factory=locks)
    used: list[Session] = []
    service = threading.Thread(target=lambda: used.append(_use_session([source])))
    service.start()  # waits without limit, creating the session
    while not manager.sessions:
        time.sleep(0.01)
    [starting] = manager.sessions
    started = time.monotonic()
    with pytest.raises(RcloneBusyError, match="busy"):
        _open([source], wait=0.3)  # an API request joining the starting session
    assert time.monotonic() - started < 2
    assert starting.state == "starting" and starting.refs == 1
    locks.gate.set()
    service.join(10)
    assert used == [starting] and starting.error is None


@pytest.mark.req("STO-20")
def test_an_abandoned_session_closes_and_releases_its_lock(make_remote: RemoteFactory) -> None:
    source = _file(make_remote())
    locks = GatedLocks()
    set_session_hooks(lock_factory=locks)
    started = time.monotonic()
    with pytest.raises(RcloneBusyError, match="busy"):
        _open([source], wait=0.3)
    assert time.monotonic() - started < 2
    [abandoned] = manager.sessions
    locks.gate.set()
    assert abandoned.done.wait(5)
    assert abandoned.rcd is None  # nobody needed it any more: rcd never started
    assert locks.left == locks.entered == [[source.lock_name]]
    assert manager.sessions == ()
    _open([source], wait=5)


@pytest.mark.req("STO-20")
def test_a_caller_retries_when_a_shorter_budget_gave_up_the_lock(
    make_remote: RemoteFactory,
) -> None:
    source = _file(make_remote())
    errors: list[BaseException] = []
    used: list[Session] = []

    def short() -> None:
        try:
            _open([source], wait=0.3)
        except RcloneBusyError as e:
            errors.append(e)

    with manager.lock_factory([source.lock_name], None):  # held "elsewhere"
        api = threading.Thread(target=short)
        api.start()
        while not manager.sessions:
            time.sleep(0.01)
        service = threading.Thread(target=lambda: used.append(_use_session([source])))
        service.start()  # joins the API's session, which gives up after 0.3s
        api.join(10)
        time.sleep(0.2)
        assert not used
    service.join(10)
    assert len(errors) == 1 and len(used) == 1 and used[0].error is None


def _use_session(sources: list[ConfigSource], **wait: float | None) -> Session:
    with manager.use(sources, **wait) as session:
        return session


@pytest.mark.req("STO-20")
def test_close_closes_every_session(make_remote: RemoteFactory) -> None:
    a, b = _file(make_remote("a")), _file(make_remote("b"))
    configure(idle=60)  # only close() closes them
    sessions = [_use_session([a]), _use_session([b])]
    assert sessions[0] is not sessions[1] and set(manager.sessions) == set(sessions)
    with manager.use([a]):
        assert manager.close(0.2) is False  # one is still in use
    assert manager.close(5) is True
    assert all(s.done.is_set() for s in sessions) and manager.sessions == ()

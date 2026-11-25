"""rclone sessions: one rcd per set of configs, shared in a process (STO-20).

Every rclone call runs inside a session (:meth:`SessionManager.use`). A
session holds the lock of each config it uses (``rclone:<block>`` or
``rclone:file:<hash>``; a Prefect global concurrency limit on a server,
process-local locks otherwise) for its whole life, writes the configs into a
private temp config, runs rcd on it, and at the end stops rcd and writes every
config rclone changed back to its block or file, even after a failure.

A session is shared by reference count by every caller in the process that
needs a subset of its configs, and closes ``idle`` seconds after its last
use. A caller needing a config the open session lacks waits for it to close
(no new callers join it meanwhile) and opens one with the union of what the
waiting callers need. The locks are entered and left in a dedicated session
thread, since a lock may have to be released by the thread that took it.
"""

from __future__ import annotations

import atexit
import contextlib
import logging
import os
import shutil
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from heliopuddle.storage.rclone import binary
from heliopuddle.storage.rclone.config import (
    BlockStore,
    ConfigSource,
    Sections,
    changed_configs,
    load_config,
    merge_configs,
    save_config,
)
from heliopuddle.storage.rclone.errors import RcloneBusyError, RcloneConfigError
from heliopuddle.storage.rclone.process import Rcd, start_rcd

__all__ = [
    "Lease",
    "LockFactory",
    "Session",
    "SessionManager",
    "manager",
    "set_session_hooks",
    "wait_limit",
]

logger = logging.getLogger(__name__)

#: ``lock_factory(lock_names, timeout)`` returns a context manager holding every
#: named lock; ``timeout`` None waits forever, otherwise a ``TimeoutError`` is raised.
LockFactory = Callable[[list[str], float | None], AbstractContextManager[Any]]

_UNSET: Any = object()
_WAIT: ContextVar[float | None] = ContextVar("heliopuddle_rclone_wait", default=None)


@contextmanager
def wait_limit(seconds: float | None) -> Iterator[None]:
    """Within this block, wait at most ``seconds`` for an rclone config lock (None: forever).

    API requests use a limit and answer ``503`` on :class:`RcloneBusyError`;
    services wait without one.
    """
    token = _WAIT.set(seconds)
    try:
        yield
    finally:
        _WAIT.reset(token)


class LocalLocks:
    """The default lock factory: one process-local lock per name."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    @contextmanager
    def __call__(self, names: list[str], timeout: float | None) -> Iterator[None]:
        deadline = None if timeout is None else time.monotonic() + timeout
        held: list[threading.Lock] = []
        try:
            for name in sorted(names):
                with self._guard:
                    lock = self._locks.setdefault(name, threading.Lock())
                remaining = -1.0 if deadline is None else max(0.0, deadline - time.monotonic())
                if not lock.acquire(timeout=remaining):
                    raise TimeoutError(f"rclone config lock {name!r} is busy")
                held.append(lock)
            yield
        finally:
            for lock in reversed(held):
                lock.release()


class Session:
    """One rcd over a set of configs. Created by :class:`SessionManager`.

    Attributes:
        sources: the configs of this session.
        remotes: the remote names they define (after start).
        directory: the private 0700 session directory (socket, config, spools).
        rcd: the running rcd (after start).
        error: why the session failed to start, if it did.
        writeback_errors: failures saving changed configs back (logged too).
    """

    def __init__(self, sources: frozenset[ConfigSource], owner: SessionManager) -> None:
        self.sources = sources
        self.remotes: frozenset[str] = frozenset()
        self.directory: Path | None = None
        self.rcd: Rcd | None = None
        self.error: BaseException | None = None
        self.writeback_errors: list[str] = []
        self.refs = 0
        self.holders: Counter[int] = Counter()
        self.state = "starting"  # -> open -> closing -> closed
        self.draining = False
        self.close_now = False
        self.last_release = time.monotonic()
        self.ready = threading.Event()
        self.done = threading.Event()
        self._owner = owner
        self._originals: dict[ConfigSource, Sections] = {}
        self._thread = threading.Thread(target=self._run, name="hp-rclone-session", daemon=True)

    def __repr__(self) -> str:
        names = ", ".join(sorted(s.describe() for s in self.sources))
        return f"Session({names}, state={self.state})"

    @property
    def client(self) -> Any:
        """The rc client of the running rcd."""
        if self.rcd is None:
            raise RcloneConfigError("the rclone session is not running")
        return self.rcd.client

    def start(self, wait: float | None) -> None:
        """Start the session thread, which takes the locks (waiting at most ``wait``)."""
        self._wait = wait
        self._thread.start()

    def _run(self) -> None:
        owner = self._owner
        locked = False
        try:
            names = sorted(s.lock_name for s in self.sources)
            with owner.lock_factory(names, self._wait):
                locked = True
                try:
                    self._start(owner.block_store)
                    with owner.cond:
                        self.state = "open"
                    self.ready.set()
                    self._idle_wait()
                finally:
                    self._shutdown(owner.block_store)
        except BaseException as e:
            if not locked and isinstance(e, TimeoutError):
                names = ", ".join(sorted(s.describe() for s in self.sources))
                e = RcloneBusyError(f"rclone {names} is in use elsewhere; try again later")
            self.error = e
        finally:
            with owner.cond:
                self.state = "closed"
                if owner.session is self:
                    owner.session = None
                owner.cond.notify_all()
            self.ready.set()
            self.done.set()

    def _start(self, store: BlockStore | None) -> None:
        executable = binary.find_rclone()
        texts = {source: load_config(source, store) for source in sorted(self.sources, key=repr)}
        merged, self._originals = merge_configs(texts)
        self.remotes = frozenset(name for s in self._originals.values() for name in s)
        self.directory = Path(tempfile.mkdtemp(prefix="hp-rclone-", dir=self._owner.tmp_dir))
        os.chmod(self.directory, 0o700)
        config = self.directory / "rclone.conf"
        fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(merged)
        self.rcd = start_rcd(executable, config, self.directory, self._owner.start_timeout)
        logger.debug("rclone session started: %r", self)

    def _idle_wait(self) -> None:
        cond = self._owner.cond
        with cond:
            while not self.close_now:
                if self.refs == 0:
                    if self.draining:
                        break
                    remaining = self.last_release + self._owner.idle - time.monotonic()
                    if remaining <= 0:
                        break
                    cond.wait(remaining)
                else:
                    cond.wait()
            self.state = "closing"

    def _shutdown(self, store: BlockStore | None) -> None:
        try:
            if self.rcd is not None:
                self.rcd.stop()
            if self.directory is not None and self._originals:
                self._write_back(store, self.directory / "rclone.conf")
        finally:
            if self.directory is not None:
                shutil.rmtree(self.directory, ignore_errors=True)
            logger.debug("rclone session closed: %r", self)

    def _write_back(self, store: BlockStore | None, config: Path) -> None:
        try:
            final = config.read_text()
        except OSError as e:
            self.writeback_errors.append(f"reading the session config failed: {e.strerror}")
            logger.warning("rclone session %r: %s", self, self.writeback_errors[-1])
            return
        for source, text in changed_configs(self._originals, final).items():
            try:
                save_config(source, store, text)
                logger.info("rclone %s changed (a refreshed token): saved", source.describe())
            except Exception as e:
                self.writeback_errors.append(
                    f"saving rclone {source.describe()} failed: {type(e).__name__}"
                )
                logger.warning("rclone session %r: %s", self, self.writeback_errors[-1])


class Lease:
    """One reference to a session; :meth:`release` it exactly once (idempotent)."""

    def __init__(self, session: Session, owner: SessionManager, thread_id: int) -> None:
        self.session = session
        self._owner = owner
        self._thread_id = thread_id
        self._released = False

    def release(self) -> None:
        """Drop the reference; the session closes ``idle`` seconds after its last one."""
        if self._released:
            return
        self._released = True
        self._owner.release(self.session, self._thread_id)


class SessionManager:
    """The process-wide owner of the rclone session (see the module docstring).

    Attributes:
        idle: seconds a session stays open after its last use.
        tmp_dir: where session directories are made (default: the system temp dir).
        start_timeout: seconds to wait for rcd to answer.
        lock_factory: the config lock (see :func:`set_session_hooks`).
        block_store: where ``config_block`` configs live, or None.
    """

    def __init__(self) -> None:
        self.cond = threading.Condition()
        self.session: Session | None = None
        self.idle = 2.0
        self.tmp_dir: str | None = None
        self.start_timeout = 30.0
        self.lock_factory: LockFactory = LocalLocks()
        self.block_store: BlockStore | None = None
        self._waiting: list[frozenset[ConfigSource]] = []

    @contextmanager
    def use(
        self, sources: Iterable[ConfigSource], wait: float | None = _UNSET
    ) -> Iterator[Session]:
        """Run the block inside a session that has every config in ``sources``.

        Args:
            sources: the configs needed.
            wait: seconds to wait for a config lock (None: forever); by default
                the :func:`wait_limit` in effect.

        Raises:
            RcloneBusyError: a lock was not free within ``wait``.
            RcloneUnavailable: no rclone binary, a missing config, or rcd failed.
            RcloneConfigError: invalid configs (e.g. a remote defined twice).
        """
        lease = self.acquire(sources, wait)
        try:
            yield lease.session
        finally:
            lease.release()

    def acquire(self, sources: Iterable[ConfigSource], wait: float | None = _UNSET) -> Lease:
        """Like :meth:`use`, for a reference held across calls (an open file)."""
        needed = frozenset(sources)
        timeout = _WAIT.get() if wait is _UNSET else wait
        deadline = None if timeout is None else time.monotonic() + timeout
        tid = threading.get_ident()
        with self.cond:
            session = self._join_or_create(needed, tid, deadline, timeout)
        # Bounded: the session thread waits for its locks at most ``timeout``
        # and for rcd at most ``start_timeout``.
        session.ready.wait()
        if session.error is not None:
            self.release(session, tid)
            raise session.error
        return Lease(session, self, tid)

    def _join_or_create(
        self,
        needed: frozenset[ConfigSource],
        tid: int,
        deadline: float | None,
        timeout: float | None,
    ) -> Session:
        registered = False
        try:
            while True:
                session = self.session
                if session is None:
                    union = needed.union(*self._waiting)
                    session = self.session = Session(union, self)
                    session.start(timeout)
                    return self._ref(session, tid)
                covers = needed <= session.sources and session.state in ("starting", "open")
                if covers and (not session.draining or session.holders[tid] > 0):
                    return self._ref(session, tid)
                if session.holders[tid] > 0 and not needed <= session.sources:
                    raise RcloneConfigError(
                        "this thread already uses an rclone session without every config it "
                        "now needs; open one session with all of them first (SessionManager.use)"
                    )
                if not needed <= session.sources:
                    session.draining = True
                if not registered:
                    self._waiting.append(needed)
                    registered = True
                self.cond.notify_all()
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise RcloneBusyError("the rclone session is busy; try again later")
                self.cond.wait(remaining)
        finally:
            if registered:
                self._waiting.remove(needed)

    @staticmethod
    def _ref(session: Session, tid: int) -> Session:
        session.refs += 1
        session.holders[tid] += 1
        return session

    def release(self, session: Session, thread_id: int) -> None:
        """Drop one reference taken by ``thread_id`` (see :class:`Lease`)."""
        with self.cond:
            session.refs -= 1
            session.holders[thread_id] -= 1
            if session.holders[thread_id] <= 0:
                del session.holders[thread_id]
            session.last_release = time.monotonic()
            self.cond.notify_all()

    def close(self, timeout: float | None = None, *, force: bool = False) -> bool:
        """Close the open session once unused (``force``: now) and wait for it.

        Returns:
            True if no session is open afterwards.
        """
        with self.cond:
            session = self.session
            if session is None:
                return True
            session.draining = True
            session.close_now = session.close_now or force
            self.cond.notify_all()
        return session.done.wait(timeout)

    def shutdown(self) -> None:
        """Close the session now, saving changed configs (atexit), within a few seconds."""
        with contextlib.suppress(Exception):
            self.close(10.0, force=True)


manager = SessionManager()
atexit.register(manager.shutdown)


def set_session_hooks(
    lock_factory: LockFactory | None = None, block_store: BlockStore | None = None
) -> None:
    """Register the config lock and the config block store (the server does this).

    Args:
        lock_factory: ``(lock_names, timeout) -> context manager`` holding every
            named lock (entered and left in one thread); None restores the
            process-local default.
        block_store: loads and saves ``config_block`` configs (``load(name)``
            raises ``LookupError`` if missing); None means none.
    """
    manager.lock_factory = lock_factory or LocalLocks()
    manager.block_store = block_store

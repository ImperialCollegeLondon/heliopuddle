"""Brute-force protection for password logins (AUTH-18).

One in-process :class:`LoginThrottle` is shared by the API's ``POST /auth/token``
and the UI's ``POST /login``. It counts consecutive failures per username and,
separately, per client address, in a fixed window. It is **per process**: with
several server processes each keeps its own counters.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import timedelta

from heliopuddle.settings import Settings

_PRUNE_ABOVE = 10_000
"""Expired entries are dropped when a table grows past this many keys."""


class LoginThrottle:
    """Counts failed logins per username and per client address.

    Args:
        max_failures: failures per username in a window before it is blocked.
        max_failures_per_ip: failures per client address before it is blocked.
        window: how long a window lasts; a block ends when its window does.
        clock: seconds from a monotonic clock; replace it in tests.
    """

    def __init__(
        self,
        max_failures: int,
        max_failures_per_ip: int,
        window: timedelta,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_failures = max_failures
        self.max_failures_per_ip = max_failures_per_ip
        self.window = window.total_seconds()
        self.clock = clock
        self._lock = threading.Lock()
        self._users: dict[str, tuple[int, float]] = {}
        self._ips: dict[str, tuple[int, float]] = {}

    def retry_after(self, username: str | None, ip: str | None) -> int | None:
        """Seconds until a login attempt is allowed again, or None when it is allowed now."""
        now = self.clock()
        with self._lock:
            waits = [
                self._wait(table, key, limit, now)
                for table, key, limit in (
                    (self._users, _norm(username), self.max_failures),
                    (self._ips, ip, self.max_failures_per_ip),
                )
                if key
            ]
        blocked = [w for w in waits if w is not None]
        return max(blocked) if blocked else None

    def _wait(
        self, table: dict[str, tuple[int, float]], key: str, limit: int, now: float
    ) -> int | None:
        entry = table.get(key)
        if entry is None:
            return None
        count, start = entry
        if now - start >= self.window:
            del table[key]
            return None
        return max(1, int(start + self.window - now + 0.999)) if count >= limit else None

    def record_failure(self, username: str | None, ip: str | None) -> None:
        """Count a failed attempt against the username and the address (either may be None)."""
        now = self.clock()
        with self._lock:
            for table, key in ((self._users, _norm(username)), (self._ips, ip)):
                if not key:
                    continue
                count, start = table.get(key, (0, now))
                if now - start >= self.window:
                    count, start = 0, now
                table[key] = (count + 1, start)
                if len(table) > _PRUNE_ABOVE:
                    self._prune(table, now)

    def record_success(self, username: str | None) -> None:
        """Forget the username's failures (the address's count is kept)."""
        with self._lock:
            self._users.pop(_norm(username) or "", None)

    def _prune(self, table: dict[str, tuple[int, float]], now: float) -> None:
        for key in [k for k, (_, start) in table.items() if now - start >= self.window]:
            del table[key]


def _norm(username: str | None) -> str | None:
    return username.strip().lower() if username else None


_throttle: LoginThrottle | None = None
_throttle_key: tuple[int, int, timedelta] | None = None
_throttle_lock = threading.Lock()


def get_throttle(settings: Settings) -> LoginThrottle:
    """The process-wide throttle, sized by ``settings`` (rebuilt if they change)."""
    global _throttle, _throttle_key
    key = (settings.login_max_failures, settings.login_max_failures_per_ip, settings.login_window)
    with _throttle_lock:
        if _throttle is None or _throttle_key != key:
            _throttle = LoginThrottle(*key)
            _throttle_key = key
        return _throttle


def reset_throttle() -> None:
    """Drop all counters (tests)."""
    global _throttle, _throttle_key
    with _throttle_lock:
        _throttle = _throttle_key = None

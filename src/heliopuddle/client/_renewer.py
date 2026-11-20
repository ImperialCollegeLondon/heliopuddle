"""Keeping an upload reservation alive while content is written."""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Iterator
from datetime import datetime
from typing import TYPE_CHECKING, BinaryIO

import httpx

from heliopuddle.client._http import aware, server_now
from heliopuddle.client.errors import HelioPuddleError

if TYPE_CHECKING:
    from heliopuddle.client._base import ClientBase


class Renewer:
    """Keeps an upload reservation alive while writing and waiting to retry complete.

    A daemon thread posts ``/uploads/{token}/renew`` every third of the lease
    (``deadline`` minus the server's time), at least :attr:`MIN_RENEW_INTERVAL`
    seconds apart. A refused renew (``404`` or ``409``), or a deadline that
    passes while renews keep failing, marks the reservation lost:
    :class:`GuardedReader` then fails the write, so it aborts without
    committing.
    """

    MIN_RENEW_INTERVAL = 1.0
    """Shortest interval between renews, in seconds."""

    def __init__(
        self,
        client: ClientBase,
        token: str,
        deadline: datetime | None,
        server_now: datetime,
    ) -> None:
        self._client = client
        self._token = token
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._request_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._lease: float | None = None
        self._expires: float | None = None
        self.error: BaseException | None = None
        self.renewals = 0
        self._set_deadline(deadline, server_now)

    def _set_deadline(self, deadline: datetime | None, server_now: datetime) -> None:
        if deadline is None:
            return
        self._lease = max((aware(deadline) - server_now).total_seconds(), 0.0)
        self._expires = time.monotonic() + self._lease

    @property
    def interval(self) -> float | None:
        """Seconds between renews, or None when the reservation has no deadline."""
        if self._lease is None:
            return None
        return max(self._lease / 3, self.MIN_RENEW_INTERVAL)

    def __enter__(self) -> Renewer:
        if self._lease is not None:
            self._thread = threading.Thread(target=self._run, name="hp-upload-renew", daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
        if self._thread is not None:
            self._thread.join()

    def stop(self) -> None:
        """Prevent further renewals once completion succeeds or the upload fails."""
        self._stop.set()

    @contextlib.contextmanager
    def completing(self) -> Iterator[None]:
        """Prevent renewals during the server's exclusive complete claim."""
        with self._request_lock:
            self.raise_if_lost()
            yield

    def wait(self, seconds: float) -> None:
        """Wait between retries while renewing, stopping promptly if ownership is lost."""
        self._lost.wait(seconds)
        self.raise_if_lost()

    def raise_if_lost(self) -> None:
        """Raise the error that lost the reservation, if any."""
        if self.error is not None:
            raise self.error

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            with self._request_lock:
                if self._stop.is_set():
                    return
                self._renew()
                if self.error is not None:
                    self._lost.set()
                    return

    def _renew(self) -> None:
        try:
            response = self._client._request(  # pyright: ignore[reportPrivateUsage]
                "POST", f"/uploads/{self._token}/renew"
            )
        except HelioPuddleError as e:
            if e.status_code in (404, 409) or self._expired():
                self.error = e
                return
            return
        except Exception as e:
            if self._expired():
                self.error = HelioPuddleError(0, f"could not renew the reservation: {e}")
                return
            return
        self.renewals += 1
        self._renewed(response)

    def _renewed(self, response: httpx.Response) -> None:
        """Take the new deadline from a renew answer (an ``UploadReservation``)."""
        with contextlib.suppress(ValueError, TypeError, AttributeError):
            deadline = response.json().get("deadline")
            if deadline is not None:
                self._set_deadline(datetime.fromisoformat(str(deadline)), server_now(response))

    def _expired(self) -> bool:
        return self._expires is not None and time.monotonic() >= self._expires


class GuardedReader:
    """A binary source that fails as soon as the reservation is lost."""

    def __init__(self, raw: BinaryIO, renewer: Renewer) -> None:
        self._raw = raw
        self._renewer = renewer

    def read(self, size: int = -1) -> bytes:
        self._renewer.raise_if_lost()
        return self._raw.read(size)

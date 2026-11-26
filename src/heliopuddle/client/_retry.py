"""Retrying a call whose path is busy (CLI-17), shared by the client and the fake."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TypeVar

from heliopuddle.client.errors import BusyError

T = TypeVar("T")

logger = logging.getLogger("heliopuddle.client")

DEFAULT_BUSY_WAIT = 1.0
"""Seconds to wait before retrying a busy path when the server gave no usable ``Retry-After``."""


def check_busy_retries(busy_retries: int) -> int:
    """Return ``busy_retries`` if it is valid.

    Raises:
        ValueError: it is negative.
    """
    if busy_retries < 0:
        raise ValueError(f"busy_retries must be 0 or more, not {busy_retries}")
    return busy_retries


def sleep(seconds: float) -> None:
    """Wait ``seconds`` (a seam for tests)."""
    time.sleep(seconds)


def call_with_busy_retries(call: Callable[[], T], busy_retries: int) -> T:
    """Run ``call``, retrying it while it raises :class:`BusyError`.

    A busy path means another operation owns it and nothing was changed, so
    the same call is safe to send again. Each retry waits the ``Retry-After``
    the server sent (:data:`DEFAULT_BUSY_WAIT` when it sent none).

    Args:
        call: the call to make.
        busy_retries: how many times to retry; 0 raises the first ``BusyError``.

    Returns:
        What ``call`` returned.

    Raises:
        BusyError: the path was still busy after ``busy_retries`` retries.
    """
    attempt = 0
    while True:
        try:
            return call()
        except BusyError as error:
            if attempt >= busy_retries:
                raise
            attempt += 1
            wait = error.retry_after if error.retry_after is not None else DEFAULT_BUSY_WAIT
            logger.info("path busy; retrying in %.1fs (%d/%d)", wait, attempt, busy_retries)
            sleep(wait)

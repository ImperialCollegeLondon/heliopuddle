"""Formatting helpers for templates: sizes, counts, times, durations, paths and names.

Pure functions; no requests and no client calls.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def human_size(size: int | None) -> str:
    """Format a byte count with binary units.

    Args:
        size: number of bytes, or None.

    Returns:
        For example ``"512 B"``, ``"1.5 KiB"`` or ``"3.0 GiB"``; empty for None.
    """
    if size is None:
        return ""
    value = float(size)
    for unit in _UNITS[:-1]:
        if value < 1024:
            return f"{size} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} {_UNITS[-1]}"


def number(value: int | None) -> str:
    """Format a count with thousands separators (``48,213``); empty for None."""
    return "" if value is None else f"{value:,}"


def format_datetime(value: datetime | None) -> str:
    """Format a timestamp as ``YYYY-MM-DD HH:MM:SS`` (empty for None)."""
    return value.strftime("%Y-%m-%d %H:%M:%S") if value is not None else ""


def format_date(value: datetime | None) -> str:
    """Format a timestamp as ``YYYY-MM-DD HH:MM``, or ``YYYY-MM-DD`` at midnight."""
    if value is None:
        return ""
    if (value.hour, value.minute, value.second) == (0, 0, 0):
        return value.strftime("%Y-%m-%d")
    return value.strftime("%Y-%m-%d %H:%M")


def relative_time(value: datetime | None, now: datetime | None = None) -> str:
    """Describe how long ago ``value`` was: ``just now``, ``12 min ago``, ``3 days ago``.

    Args:
        value: the timestamp (naive values are taken as UTC).
        now: the current time; defaults to now.

    Returns:
        A short phrase, or an empty string for None. Future times read ``in …``.
    """
    if value is None:
        return ""
    now = now or datetime.now(UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    seconds = (now - value).total_seconds()
    future = seconds < 0
    seconds = abs(seconds)
    if seconds < 60:
        return "just now"
    for limit, size, unit in (
        (3600, 60, "min"),
        (86400, 3600, "h"),
        (86400 * 45, 86400, "day"),
        (86400 * 548, 86400 * 30.44, "month"),
        (math.inf, 86400 * 365.25, "year"),
    ):
        if seconds < limit:
            count = int(seconds // size)
            label = unit if unit in ("min", "h") or count == 1 else unit + "s"
            return f"in {count} {label}" if future else f"{count} {label} ago"
    return ""  # pragma: no cover - the last limit is infinite


def format_duration(value: timedelta) -> str:
    """A duration in the config's own form: ``30d``, ``12h``, ``45m`` or ``60s``."""
    seconds = int(value.total_seconds())
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds and seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def metadata_value(value: Any) -> str:
    """Render a metadata value: strings as they are, anything else as indented JSON."""
    if isinstance(value, str):
        return value
    return json.dumps(value, indent=2, sort_keys=True, default=str)


def split_path(path: str) -> tuple[str, str]:
    """Split a path into its folder (with a trailing ``/``) and its name."""
    folder, _, name = path.rpartition("/")
    return (folder + "/" if folder else ""), name


def initials(username: str) -> str:
    """Up to two initials for a user avatar (``ann.lee`` gives ``AL``)."""
    parts = [p for p in username.replace(".", " ").replace("-", " ").replace("_", " ").split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()


def scheme(url: str) -> str:
    """The scheme of a storage-location URL in upper case (``FILE``), or empty if withheld."""
    if not url:
        return ""
    return (urlsplit(url).scheme or "file").upper()

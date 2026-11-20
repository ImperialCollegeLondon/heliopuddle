"""Human-readable durations: ``30d``, ``12h``, ``45m``, ``60s``, ``1h30m``, ``1y6mo``.

See docs/design/rules.md ("Durations") and DEP-19.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Annotated

from pydantic import BeforeValidator, PlainSerializer

_UNIT_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 7 * 86400,
    "mo": 30 * 86400,
    "y": 365 * 86400,
}
"""Seconds per unit; a month is 30 days and a year 365 days."""

_ALIASES = {
    **{a: "s" for a in ("s", "sec", "secs", "second", "seconds")},
    **{a: "m" for a in ("m", "min", "mins", "minute", "minutes")},
    **{a: "h" for a in ("h", "hr", "hrs", "hour", "hours")},
    **{a: "d" for a in ("d", "day", "days")},
    **{a: "w" for a in ("w", "wk", "wks", "week", "weeks")},
    **{a: "mo" for a in ("mo", "mos", "month", "months")},
    **{a: "y" for a in ("y", "yr", "yrs", "year", "years")},
}

_PART = re.compile(r"(\d+(?:\.\d+)?)\s*([a-z]+)")
_HINT = "use e.g. 30d, 12h, 45m, 60s, 1h30m, 2w or 1y6mo"


def parse_duration(value: str | int | float | timedelta) -> timedelta:
    """Parse a duration such as ``"30d"``, ``"1h30m"``, ``"1y6mo"`` or a number of seconds.

    Units: ``s``, ``m`` (minutes), ``h``, ``d``, ``w``, ``mo`` (30 days) and
    ``y`` (365 days), or their long names (``minutes``, ``months``, ...),
    case-insensitive, optionally separated by spaces.

    Raises:
        ValueError: if ``value`` is not a valid, non-negative duration.
    """
    try:
        if isinstance(value, timedelta):
            if value < timedelta():
                raise ValueError("duration must not be negative")
            return value
        if isinstance(value, int | float) and not isinstance(value, bool):
            if value < 0:
                raise ValueError("duration must not be negative")
            return timedelta(seconds=value)
        text = str(value).strip().lower()
        parts = _PART.findall(text)
        if not text or _PART.sub("", text).strip() or not parts:
            raise ValueError(f"invalid duration {value!r}; {_HINT}")
        total = timedelta()
        for amount, word in parts:
            unit = _ALIASES.get(word)
            if unit is None:
                raise ValueError(f"invalid duration {value!r}: unknown unit {word!r}; {_HINT}")
            number = float(amount) if "." in amount else int(amount)
            total += timedelta(seconds=number * _UNIT_SECONDS[unit])
        return total
    except OverflowError as error:
        raise ValueError("duration is outside the supported range") from error


def format_duration(value: timedelta) -> str:
    """Format a duration as ``1d2h3m4.5s``, preserving microseconds (``0s`` for zero)."""
    if value < timedelta():
        raise ValueError("duration must not be negative")
    seconds, micros = divmod(value // timedelta(microseconds=1), 1_000_000)
    out = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        amount, seconds = divmod(seconds, size)
        if amount:
            out.append(f"{amount}{unit}")
    if micros:
        out.append(f"{seconds}.{micros:06d}".rstrip("0") + "s")
    elif seconds:
        out.append(f"{seconds}s")
    return "".join(out) or "0s"


Duration = Annotated[
    timedelta,
    BeforeValidator(parse_duration),
    PlainSerializer(format_duration, return_type=str),
]
"""A pydantic field type accepting human-readable durations."""

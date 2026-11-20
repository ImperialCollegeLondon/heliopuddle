"""DEP-19: human-readable durations in config, settings and the CLI."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from heliopuddle.config.models import IndexConfig
from heliopuddle.durations import format_duration, parse_duration
from heliopuddle.settings import Settings

pytestmark = pytest.mark.req("DEP-19")

DAY = timedelta(days=1)
MONTH = 30 * DAY
YEAR = 365 * DAY

UNITS: dict[timedelta, tuple[str, ...]] = {
    timedelta(seconds=1): ("s", "sec", "secs", "second", "seconds"),
    timedelta(minutes=1): ("m", "min", "mins", "minute", "minutes"),
    timedelta(hours=1): ("h", "hr", "hrs", "hour", "hours"),
    DAY: ("d", "day", "days"),
    timedelta(weeks=1): ("w", "wk", "wks", "week", "weeks"),
    MONTH: ("mo", "mos", "month", "months"),
    YEAR: ("y", "yr", "yrs", "year", "years"),
}


@pytest.mark.parametrize(
    ("unit", "size"), [(unit, size) for size, units in UNITS.items() for unit in units]
)
@pytest.mark.parametrize("template", ["{n}{unit}", "{n} {unit}", "{n}{UNIT}", " {n}  {Unit} "])
def test_every_unit_and_alias_in_any_case_and_spacing(
    unit: str, size: timedelta, template: str
) -> None:
    text = template.format(n=3, unit=unit, UNIT=unit.upper(), Unit=unit.capitalize())
    assert parse_duration(text) == 3 * size


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("30d", 30 * DAY),
        ("12h", timedelta(hours=12)),
        ("45m", timedelta(minutes=45)),
        ("60s", timedelta(seconds=60)),
        ("1h30m", timedelta(hours=1, minutes=30)),
        ("2h 30m", timedelta(hours=2, minutes=30)),
        ("90 days", 90 * DAY),
        ("1y6mo", YEAR + 6 * MONTH),
        ("3 Months 2 days", 3 * MONTH + 2 * DAY),
        ("1 year 2 weeks 3 days 4 hours 5 minutes 6 seconds", YEAR + timedelta(17, 14706)),
        ("2W", timedelta(weeks=2)),
        ("1mo1m", MONTH + timedelta(minutes=1)),  # mo is months, m is minutes
        ("1m1mo", MONTH + timedelta(minutes=1)),
        ("1h1h", timedelta(hours=2)),  # repeated units add up
        ("0s", timedelta()),
        ("0d", timedelta()),
        ("1.5h", timedelta(hours=1, minutes=30)),
        ("0.5d", timedelta(hours=12)),
        ("0.5mo", 15 * DAY),
        ("0.25s", timedelta(milliseconds=250)),
        ("1.5 hours 30 min", timedelta(hours=2)),
        (90, timedelta(seconds=90)),
        (0.5, timedelta(milliseconds=500)),
        (0, timedelta()),
        (timedelta(days=2), timedelta(days=2)),
    ],
)
def test_combinations_fractions_and_numbers(
    text: str | int | float | timedelta, expected: timedelta
) -> None:
    assert parse_duration(text) == expected


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        ("", "invalid duration"),
        ("   ", "invalid duration"),
        ("5x", "unknown unit 'x'"),
        ("1ms", "unknown unit 'ms'"),
        ("1 fortnight", "unknown unit 'fortnight'"),
        ("1e3s", "unknown unit 'e'"),
        ("h", "invalid duration"),
        ("days", "invalid duration"),
        ("30", "invalid duration"),  # a string needs a unit
        ("1h junk", "invalid duration"),
        ("1h,30m", "invalid duration"),
        ("1h-30m", "invalid duration"),
        ("-1d", "invalid duration"),
        ("+1d", "invalid duration"),
        ("1.d", "invalid duration"),
        (".5d", "invalid duration"),
        ("1..5d", "invalid duration"),
        ("soon", "invalid duration"),
        (-1, "must not be negative"),
        (-0.5, "must not be negative"),
        (timedelta(seconds=-1), "must not be negative"),
        (float("inf"), "outside the supported range"),
        ("999999999y", "outside the supported range"),
    ],
)
def test_errors(bad: str | int | float | timedelta, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_duration(bad)


@pytest.mark.parametrize("bad", ["", "5x", "-1d", "1h junk"])
def test_errors_give_an_example_of_the_syntax(bad: str) -> None:
    with pytest.raises(ValueError, match=r"use e\.g\. 30d, 12h, 45m, 60s, 1h30m, 2w or 1y6mo"):
        parse_duration(bad)


def test_bool_is_not_a_duration() -> None:
    with pytest.raises(ValueError):
        parse_duration(True)


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (timedelta(), "0s"),
        (timedelta(seconds=59), "59s"),
        (timedelta(minutes=90), "1h30m"),
        (timedelta(days=1, hours=2, seconds=3), "1d2h3s"),
        (timedelta(weeks=2), "14d"),
        (MONTH, "30d"),
        (YEAR + 6 * MONTH, "545d"),
        (timedelta(milliseconds=250), "0.25s"),
        (timedelta(days=1, microseconds=1), "1d0.000001s"),
    ],
)
def test_format_duration_writes_days_hours_minutes_seconds(value: timedelta, text: str) -> None:
    assert format_duration(value) == text
    assert parse_duration(text) == value


@pytest.mark.parametrize(
    "text",
    [
        "1y6mo",
        "3 Months 2 days",
        "2h 30m",
        "90 days",
        "1.5h",
        "2w",
        "1 year 1 second",
        "0.000001s",
        "7 weeks 3 hrs 20 secs",
    ],
)
def test_round_trips_through_format_duration(text: str) -> None:
    value = parse_duration(text)
    written = format_duration(value)
    assert parse_duration(written) == value
    assert format_duration(parse_duration(written)) == written


def test_format_duration_rejects_negative() -> None:
    with pytest.raises(ValueError, match="negative"):
        format_duration(timedelta(seconds=-1))


@pytest.mark.parametrize(
    ("text", "expected"),
    [("2w", timedelta(weeks=2)), ("1 month", MONTH), ("90 Minutes", timedelta(minutes=90))],
)
def test_config_durations_accept_the_new_units(text: str, expected: timedelta) -> None:
    cfg = IndexConfig.model_validate({"settle_time": text})
    assert cfg.settle_time == expected
    assert IndexConfig.model_validate(cfg.model_dump(mode="json")).settle_time == expected


def test_env_settings_durations_accept_the_new_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HELIOPUDDLE_AUTH_TOKEN_TTL", "1 week")
    monkeypatch.setenv("HELIOPUDDLE_OP_LEASE", "2 Hours 30 min")
    settings = Settings(config_dir=tmp_path / "c", data_dir=tmp_path / "d")  # type: ignore[call-arg]
    assert settings.auth_token_ttl == timedelta(weeks=1)
    assert settings.op_lease == timedelta(hours=2, minutes=30)
    monkeypatch.setenv("HELIOPUDDLE_OP_LEASE", "2 fortnights")
    with pytest.raises(ValueError, match="unknown unit 'fortnights'"):
        Settings(config_dir=tmp_path / "c", data_dir=tmp_path / "d")  # type: ignore[call-arg]

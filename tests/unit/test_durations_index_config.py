"""DEP-07 (config part): index config validation; durations."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from heliopuddle.config.models import IndexConfig, ProjectConfig
from heliopuddle.durations import format_duration, parse_duration
from heliopuddle.server.services.index_names import (
    index_deployment_full_name,
    is_index_deployment_name,
    storage_location_lock,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("30d", timedelta(days=30)),
        ("12h", timedelta(hours=12)),
        ("45m", timedelta(minutes=45)),
        ("60s", timedelta(seconds=60)),
        ("1h30m", timedelta(hours=1, minutes=30)),
        (90, timedelta(seconds=90)),
        (timedelta(1), timedelta(1)),
    ],
)
def test_parse_duration(text: str | int | timedelta, expected: timedelta) -> None:
    assert parse_duration(text) == expected


@pytest.mark.parametrize("bad", ["", "5x", "h", "1h junk", -1])
def test_parse_duration_rejects(bad: str | int) -> None:
    with pytest.raises(ValueError):
        parse_duration(bad)


def test_format_duration() -> None:
    assert format_duration(timedelta(days=1, hours=2, seconds=3)) == "1d2h3s"
    assert format_duration(timedelta()) == "0s"


@pytest.mark.req("DEP-07")
def test_index_defaults() -> None:
    cfg = ProjectConfig.model_validate(
        {"project": "p", "storage_locations": [{"name": "s", "url": "file:///x"}]}
    )
    assert cfg.index.batch_size == 1
    assert cfg.index.settle_time == timedelta(seconds=60)
    assert cfg.index.full_schedule is None


@pytest.mark.req("DEP-07")
@pytest.mark.parametrize(
    ("data", "field"),
    [
        ({"full_schedule": "not cron"}, "full_schedule"),
        ({"incremental_schedule": "61 * * * *"}, "incremental_schedule"),
        ({"batch_size": 0}, "batch_size"),
        ({"settle_time": "soon"}, "settle_time"),
        ({"incremental_prefixes": ["a/{project}"]}, "incremental_prefixes"),
        ({"incremental_prefixes": ["a/{date:}"]}, "incremental_prefixes"),
        ({"incremental_prefixes": ["../a"]}, "incremental_prefixes"),
        ({"unknown_key": 1}, "unknown_key"),
    ],
)
def test_invalid_index_config(data: dict[str, object], field: str) -> None:
    with pytest.raises(ValidationError) as e:
        IndexConfig.model_validate(data)
    assert field in str(e.value)


@pytest.mark.req("SVC-13")
def test_index_names() -> None:
    assert index_deployment_full_name("full", 1, 2, "demo", "primary") == (
        "index_full/index-full-p1-sl2-demo-primary"
    )
    # Names that used to collide differ by their ids.
    assert index_deployment_full_name("full", 1, 1, "alpha-beta", "primary") != (
        index_deployment_full_name("full", 2, 2, "alpha", "beta-primary")
    )
    assert is_index_deployment_name("full", "index-full-p1-sl2-demo-primary")
    assert is_index_deployment_name("full", "index-full-demo-primary")  # older format
    assert not is_index_deployment_name("full", "index-incremental-p1-sl2-demo-primary")
    assert storage_location_lock("demo", "primary") == "storage-location:demo:primary"


@pytest.mark.req("DEP-12")
@pytest.mark.parametrize(
    "template",
    [
        "science/{date:%Y/%m/%d",  # unmatched brace
        "science/date:%Y}/x",  # stray closing brace
        "science/{date:%Y}{",
        "{date:..}/x",  # expands outside the storage location
        "{date:/%Y}",  # expands to an absolute path
    ],
)
def test_prefix_templates_are_parsed_at_load_time(template: str) -> None:
    with pytest.raises(ValidationError) as e:
        IndexConfig.model_validate({"incremental_prefixes": [template]})
    assert "incremental_prefixes" in str(e.value) and template in str(e.value)


@pytest.mark.req("DEP-12")
def test_valid_prefix_templates_still_load() -> None:
    cfg = IndexConfig.model_validate(
        {"incremental_prefixes": ["science/{date:%Y/%m/%d}", "raw/{date:%Y%j}", "fixed/dir"]}
    )
    assert len(cfg.incremental_prefixes) == 3


@pytest.mark.req("DEP-12")
def test_bad_prefix_template_is_a_config_error_naming_file_and_field(tmp_path: Path) -> None:
    from heliopuddle.config.loader import ConfigError, load_project

    path = tmp_path / "p.yaml"
    path.write_text(
        "project: p\n"
        "storage_locations:\n"
        "  - name: s\n"
        "    url: file:///x\n"
        "index:\n"
        "  incremental_prefixes: ['science/{date:%Y/%m']\n"
    )
    with pytest.raises(ConfigError) as e:
        load_project(path)
    message = str(e.value)
    assert str(path) in message and "incremental_prefixes" in message

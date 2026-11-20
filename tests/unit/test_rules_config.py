"""M4 rule, size and bounded-run configuration (DEP-13)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from heliopuddle.config import models
from heliopuddle.config.loader import ConfigError, load_project


def project_data(rule: dict[str, Any]) -> dict[str, Any]:
    """A two-storage-location project containing one rule."""
    return {
        "project": "demo",
        "storage_locations": [
            {"name": "primary", "url": "memory://primary"},
            {"name": "archive", "url": "memory://archive", "quota": "500GB"},
        ],
        "rules": [rule],
    }


def minimal_rule() -> dict[str, Any]:
    """The documented minimum copy rule."""
    return {
        "name": "archive",
        "action": "copy",
        "source": "primary",
        "destination": "archive",
        "match": {"paths": ["data/**"]},
    }


@pytest.mark.req("DEP-13")
def test_minimal_rule_and_index_are_bounded_with_working_defaults() -> None:
    cfg = models.ProjectConfig.model_validate(project_data(minimal_rule()))
    rule = cfg.rule("archive")
    assert rule.enabled and rule.schedule is None
    assert rule.options.check_for_space and rule.options.retrigger
    assert rule.options.check_for_space_overhead == 2_000_000_000
    assert rule.options.pause_between_runs == timedelta(seconds=30)
    assert not rule.options.overwrite and not rule.options.dry_run
    assert not rule.options.update_only_if_newer
    assert rule.match.age == models.AgeCriteria() and rule.match.age.items() == []
    assert rule.options.batch == cfg.index.batch
    assert cfg.index.batch.max_files == 100
    assert cfg.index.batch.max_bytes == 1_000_000_000
    assert cfg.index.batch.max_duration == timedelta(minutes=30)
    assert cfg.storage_location("archive").quota == 500_000_000_000
    assert models.ProjectConfig.model_validate_json(cfg.model_dump_json()) == cfg


@pytest.mark.req("DEP-13", "API-04")
def test_one_off_uses_same_validation_without_name_or_schedule() -> None:
    spec = models.RuleSpec.model_validate(
        {
            "action": "delete",
            "source": "primary",
            "match": {
                "paths": ["**/*.dat"],
                "age": {
                    "modified": {"older_than": "1h"},
                    "content_date": {"younger_than": "90d", "older_than": "2026-01-02"},
                },
            },
            "options": {
                "batch": {"max_files": None, "max_bytes": "1GiB", "max_duration": None},
                "check_for_space_overhead": "0B",
            },
        }
    )
    content_date = spec.match.age.content_date
    assert content_date == models.AgeRange(
        younger_than=timedelta(days=90), older_than=datetime(2026, 1, 2, tzinfo=UTC)
    )
    assert spec.options.batch.max_bytes == 1024**3
    assert models.RuleSpec.model_validate_json(spec.model_dump_json()) == spec
    cfg = models.ProjectConfig.model_validate(project_data(minimal_rule()))
    cfg.validate_rule(spec)


@pytest.mark.req("DEP-13", "API-04")
def test_positive_subsecond_limits_survive_submission_serialization() -> None:
    data = project_data(minimal_rule())
    data["rules"][0]["options"] = {
        "batch": {"max_duration": 0.5},
        "pause_between_runs": 0.25,
    }
    cfg = models.ProjectConfig.model_validate(data)
    restored = models.ProjectConfig.model_validate_json(cfg.model_dump_json())
    assert restored == cfg
    assert restored.rule("archive").options.batch.max_duration == timedelta(milliseconds=500)


@pytest.mark.req("DEP-13")
def test_overflowing_duration_reports_config_file_and_field(tmp_path: Path) -> None:
    data = project_data(minimal_rule())
    data["rules"][0]["options"] = {"batch": {"max_duration": float("inf")}}
    path = tmp_path / "demo.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError) as error:
        load_project(path)
    assert str(path) in str(error.value)
    assert "rules.0.options.batch.max_duration" in str(error.value)


@pytest.mark.req("DEP-13")
@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"source": "absent"}, "source"),
        ({"destination": "absent"}, "destination"),
        ({"destination": "primary"}, "differ"),
        ({"destination": None}, "destination"),
        ({"action": "delete"}, "destination"),
        ({"action": "mirror"}, "action"),
        ({"name": "bad name"}, "name"),
        ({"schedule": "61 * * * *"}, "schedule"),
        ({"match": {"paths": []}}, "paths"),
        ({"match": {"paths": ["../**"]}}, "paths"),
        ({"match": {"paths": ["/data/**"]}}, "paths"),
        ({"match": {"paths": [""]}}, "paths"),
        ({"match": {"paths": ["**"], "age": {"modified": {"older_than": "-2d"}}}}, "older_than"),
        ({"match": {"paths": ["**"], "age": {"born": {"older_than": "1d"}}}}, "age.born"),
        (
            {"match": {"paths": ["**"], "age": {"content_date": {"younger_than": "yesterday"}}}},
            "content_date.younger_than",
        ),
        ({"options": {"max_files": 2}}, "max_files"),
        ({"options": {"batch": {"max_files": 0}}}, "max_files"),
        ({"options": {"batch": {"max_files": True}}}, "max_files"),
        ({"options": {"batch": {"max_bytes": "0GB"}}}, "max_bytes"),
        ({"options": {"batch": {"max_duration": "0s"}}}, "max_duration"),
        ({"options": {"check_for_space_overhead": "-1GB"}}, "check_for_space_overhead"),
        ({"options": {"pause_between_runs": "-1s"}}, "pause_between_runs"),
    ],
)
def test_invalid_rules_fail_with_the_file_and_field(
    tmp_path: Path, patch: dict[str, Any], message: str
) -> None:
    path = tmp_path / "demo.yaml"
    path.write_text(yaml.safe_dump(project_data(minimal_rule() | patch)))
    with pytest.raises(ConfigError) as error:
        load_project(path)
    assert str(path) in str(error.value)
    assert message in str(error.value)


@pytest.mark.req("DEP-13")
def test_duplicate_rules_and_unbounded_runs_are_rejected() -> None:
    data = project_data(minimal_rule())
    data["rules"].append(minimal_rule())
    with pytest.raises(ValidationError, match="duplicate rule"):
        models.ProjectConfig.model_validate(data)
    with pytest.raises(ValidationError, match="at least one"):
        models.BatchConfig(max_files=None, max_bytes=None, max_duration=None)


@pytest.mark.req("DEP-13")
@pytest.mark.parametrize("quota", ["0GB", "-2GB", "many", True])
def test_invalid_quota_is_rejected(quota: object) -> None:
    data = project_data(minimal_rule())
    data["storage_locations"][1]["quota"] = quota
    with pytest.raises(ValidationError, match="quota"):
        models.ProjectConfig.model_validate(data)


@pytest.mark.req("DEP-13")
def test_yaml_dates_and_delete_without_destination_are_valid(tmp_path: Path) -> None:
    data = project_data(minimal_rule())
    rule = data["rules"][0]
    rule.pop("destination")
    rule["action"] = "delete"
    rule["match"]["age"] = {"content_date": {"younger_than": datetime(2026, 1, 1).date()}}
    path = tmp_path / "demo.yaml"
    path.write_text(yaml.safe_dump(data))
    cfg = load_project(path)
    age = cfg.rule("archive").match.age
    assert age.content_date == models.AgeRange(younger_than=datetime(2026, 1, 1, tzinfo=UTC))
    with pytest.raises(KeyError):
        cfg.rule("absent")


# --- M6A age criteria (DEP-13) ----------------------------------------------------------


def age_rule(age: Any) -> dict[str, Any]:
    """The minimal rule with ``match.age`` set."""
    return minimal_rule() | {"match": {"paths": ["data/**"], "age": age}}


def load_yaml(tmp_path: Path, text: str) -> models.ProjectConfig:
    """Load a project whose one rule has the given ``match:`` YAML block."""
    path = tmp_path / "demo.yaml"
    path.write_text(
        "project: demo\n"
        "storage_locations:\n"
        "  - {name: primary, url: 'memory://primary'}\n"
        "  - {name: archive, url: 'memory://archive'}\n"
        "rules:\n"
        "  - name: archive\n"
        "    action: copy\n"
        "    source: primary\n"
        "    destination: archive\n"
        "    match:\n" + text
    )
    return load_project(path)


@pytest.mark.req("DEP-13")
@pytest.mark.parametrize(
    ("key", "value", "hint"),
    [
        ("older_than", "30d", "age: {modified: {older_than: ...}}"),
        ("age_by", "content_date", "age: {content_date: {...}} or age: {modified: {...}}"),
        ("content_date_after", "-90d", "age: {content_date: {younger_than: ...}}"),
        ("content_date_before", "2026-01-01", "age: {content_date: {older_than: ...}}"),
    ],
)
def test_removed_match_keys_name_file_rule_field_and_replacement(
    tmp_path: Path, key: str, value: str, hint: str
) -> None:
    rule = minimal_rule()
    rule["match"][key] = value
    path = tmp_path / "demo.yaml"
    path.write_text(yaml.safe_dump(project_data(rule)))
    with pytest.raises(ConfigError) as error:
        load_project(path)
    message = str(error.value)
    assert str(path) in message
    assert "rule 'archive': rules.0.match" in message
    assert f"{key!r} was removed" in message
    assert hint in message
    with pytest.raises(ValidationError, match="was removed"):
        models.RuleSpec.model_validate(
            {"action": "copy", "source": "a", "destination": "b"} | {"match": rule["match"]}
        )


@pytest.mark.req("DEP-13")
@pytest.mark.parametrize(
    ("age", "field", "message"),
    [
        ({"born": {"older_than": "1d"}}, "age.born", "Extra inputs are not permitted"),
        ({"modified": {"newer_than": "1d"}}, "modified.newer_than", "Extra inputs"),
        ({"modified": {}}, "age.modified", "give older_than, younger_than or both"),
        ({"modified": None}, None, None),  # explicitly unset is fine
        (
            {"modified": {"older_than": "7d", "younger_than": "7d"}},
            "age.modified",
            "older_than 7d must be shorter than younger_than 7d, or nothing can match",
        ),
        (
            {"arrived": {"older_than": "30d", "younger_than": "1d"}},
            "age.arrived",
            "older_than 30d must be shorter than younger_than 1d",
        ),
        (
            {"content_date": {"older_than": "2024-01-01", "younger_than": "2025-01-01"}},
            "age.content_date",
            "older_than 2024-01-01T00:00:00+00:00 must be later than younger_than",
        ),
        (
            {"created": {"older_than": "2025-01-01", "younger_than": "2025-01-01T00:00:00Z"}},
            "age.created",
            "must be later than",
        ),
        ({"modified": {"older_than": "-1d"}}, "modified.older_than", "invalid age bound"),
        ({"modified": {"older_than": "yesterday"}}, "modified.older_than", "invalid age bound"),
        ({"modified": {"older_than": "5x"}}, "modified.older_than", "1y6mo"),
        ({"modified": {"older_than": True}}, "modified.older_than", "ISO date"),
        ({"modified": {"younger_than": -5}}, "modified.younger_than", "negative"),
        ({"modified": {"younger_than": ["1d"]}}, "modified.younger_than", "ISO date"),
        ({"modified": {"older_than": "2025-13-01"}}, "modified.older_than", "invalid age bound"),
        ("30d", "match.age", "valid dictionary"),
    ],
)
def test_invalid_age_criteria_name_file_rule_and_field(
    tmp_path: Path, age: Any, field: str | None, message: str | None
) -> None:
    path = tmp_path / "demo.yaml"
    path.write_text(yaml.safe_dump(project_data(age_rule(age))))
    if field is None:
        assert load_project(path).rule("archive").match.age == models.AgeCriteria()
        return
    with pytest.raises(ConfigError) as error:
        load_project(path)
    text = str(error.value)
    assert str(path) in text and "rule 'archive': rules.0.match" in text
    assert field in text
    assert message is not None and message in text


@pytest.mark.req("DEP-13")
@pytest.mark.parametrize(
    "bounds",
    [
        {"older_than": "1d", "younger_than": "2025-01-01"},  # mixed kinds: checked at run time
        {"older_than": "2099-01-01", "younger_than": "30d"},
        {"older_than": "1d", "younger_than": "1d1s"},
        {"older_than": "2025-01-01T00:00:01", "younger_than": "2025-01-01"},
        {"older_than": "0s"},
        {"younger_than": "1y6mo"},
    ],
)
def test_possible_and_mixed_ranges_are_accepted(bounds: dict[str, str]) -> None:
    cfg = models.ProjectConfig.model_validate(
        project_data(age_rule({"modified": bounds, "arrived": bounds}))
    )
    age = cfg.rule("archive").match.age
    assert [field for field, _ in age.items()] == ["modified", "arrived"]
    assert age.modified == age.arrived


@pytest.mark.req("DEP-13")
def test_every_age_field_with_durations_and_dates(tmp_path: Path) -> None:
    cfg = load_yaml(
        tmp_path,
        "      paths: ['**']\n"
        "      age:\n"
        "        arrived: {older_than: 3 days}\n"
        "        content_date: {older_than: 2025-06-01, younger_than: 2024-01-01}\n"
        "        created: {younger_than: 2025-01-02T03:04:05}\n"
        "        modified: {older_than: 1h30m, younger_than: 1y6mo}\n",
    )
    age = cfg.rule("archive").match.age
    # YAML dates and naive datetimes are UTC.
    assert age.content_date == models.AgeRange(
        older_than=datetime(2025, 6, 1, tzinfo=UTC), younger_than=datetime(2024, 1, 1, tzinfo=UTC)
    )
    assert age.created == models.AgeRange(younger_than=datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC))
    assert age.modified == models.AgeRange(
        older_than=timedelta(hours=1, minutes=30), younger_than=timedelta(days=545)
    )
    assert age.arrived == models.AgeRange(older_than=timedelta(days=3))
    assert [field for field, _ in age.items()] == list(models.AGE_FIELDS)


@pytest.mark.req("DEP-13")
def test_aware_datetimes_are_converted_to_utc(tmp_path: Path) -> None:
    cfg = load_yaml(
        tmp_path,
        "      paths: ['**']\n"
        "      age:\n"
        "        modified: {older_than: '2025-01-01T02:00:00+02:00'}\n"
        "        created: {older_than: 2025-01-01T00:00:00Z}\n",
    )
    age = cfg.rule("archive").match.age
    assert age.modified is not None and age.modified.older_than == datetime(2025, 1, 1, tzinfo=UTC)
    assert age.created is not None and age.created.older_than == datetime(2025, 1, 1, tzinfo=UTC)
    assert isinstance(age.modified.older_than, datetime)
    assert age.modified.older_than.utcoffset() == timedelta()


@pytest.mark.req("DEP-13", "API-04")
def test_age_criteria_survive_json_round_trips() -> None:
    spec = models.RuleSpec.model_validate(
        {
            "action": "copy",
            "source": "primary",
            "destination": "archive",
            "match": {
                "paths": ["**"],
                "age": {
                    "modified": {"older_than": "1h30m", "younger_than": "1y6mo"},
                    "created": {"older_than": "2025-01-01"},
                    "content_date": {"older_than": "2d", "younger_than": "2024-01-01T12:00:00"},
                    "arrived": {"older_than": 0.5},
                },
            },
        }
    )
    dumped = spec.model_dump(mode="json")  # what Prefect parameters carry
    assert dumped["match"]["age"] == {
        "modified": {"older_than": "1h30m", "younger_than": "545d"},
        "created": {"older_than": "2025-01-01T00:00:00+00:00", "younger_than": None},
        "content_date": {"older_than": "2d", "younger_than": "2024-01-01T12:00:00+00:00"},
        "arrived": {"older_than": "0.5s", "younger_than": None},
    }
    assert models.RuleSpec.model_validate(dumped) == spec
    assert models.RuleSpec.model_validate_json(spec.model_dump_json()) == spec
    assert models.RuleSpec.model_validate(yaml.safe_load(yaml.safe_dump(dumped))) == spec


@pytest.mark.req("DEP-13")
def test_age_cutoff_resolves_durations_against_selection_time() -> None:
    now = datetime(2026, 3, 1, tzinfo=UTC)
    assert models.age_cutoff(timedelta(days=1), now) == datetime(2026, 2, 28, tzinfo=UTC)
    fixed = datetime(2025, 1, 1, tzinfo=UTC)
    assert models.age_cutoff(fixed, now) == fixed


@pytest.mark.req("DEP-13")
def test_a_rule_without_a_name_is_reported_by_position(tmp_path: Path) -> None:
    rule = minimal_rule()
    rule.pop("name")
    path = tmp_path / "demo.yaml"
    path.write_text(yaml.safe_dump(project_data(rule)))
    with pytest.raises(ConfigError) as error:
        load_project(path)
    assert f"{path}: rules.0.name: Field required" in str(error.value)

"""Versioning: invalid versions, rule validation and globs."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from heliopuddle.config.models import VersionRule
from heliopuddle.globs import glob_match, literal_prefix
from heliopuddle.versioning import (
    MAX_VERSION_NUMBER,
    MAX_VERSION_TEXT,
    InvalidVersionError,
    derive,
)
from tests.unit._versioning_helpers import (
    _rule,
)

# --- DEP-12: invalid extracted versions ---------------------------------------------


@pytest.mark.req("DEP-12")
@pytest.mark.parametrize(
    ("rule", "path", "group", "value", "reason"),
    [
        (_rule(r"_(?P<version>[a-z0-9]+)\.dat$"), "x_draft.dat", "version", "draft", "integer"),
        (_rule(r"_(?P<version>\S+)\.dat$"), "x_١٢.dat", "version", "١٢", "integer"),
        (_rule(r"_[a-z0-9]+(?=\.dat$)"), "x_v1.dat", None, "_v1", "integer"),
        (
            _rule(r"_(?P<version>\d+)\.dat$"),
            f"x_{MAX_VERSION_NUMBER + 1}.dat",
            "version",
            str(MAX_VERSION_NUMBER + 1),
            "too large",
        ),
        (
            _rule(r"_(?P<version>[a-z]*)\.dat$", version_type="text"),
            "x_.dat",
            "version",
            "",
            "empty",
        ),
        (
            _rule(r"_(?P<a>[a-z]*)\.dat$", version_type="text", version_text="{a}"),
            "x_.dat",
            None,
            "",
            "empty",
        ),
        (
            _rule(r"_(?P<version>[a-z]+)\.dat$", version_type="text"),
            "x_" + "a" * (MAX_VERSION_TEXT + 1) + ".dat",
            "version",
            "a" * (MAX_VERSION_TEXT + 1),
            "longer than 255",
        ),
    ],
)
def test_invalid_extracted_version_names_rule_and_value(
    rule: VersionRule, path: str, group: str | None, value: str, reason: str
) -> None:
    other = _rule(r"_k(?P<version>\d+)\.dat$", paths=["other/**"])
    with pytest.raises(InvalidVersionError) as err:
        derive(path, [other, rule])
    assert (err.value.rule_index, err.value.group, err.value.value) == (1, group, value)
    assert "rule 1" in str(err.value)
    assert repr(value) in str(err.value)
    assert reason in str(err.value)
    assert isinstance(err.value, ValueError)


@pytest.mark.req("DEP-12")
def test_largest_values_are_valid() -> None:
    number = _rule(r"_(?P<version>\d+)\.dat$")
    assert derive(f"x_{MAX_VERSION_NUMBER}.dat", [number]).version_number == MAX_VERSION_NUMBER
    text = _rule(r"_(?P<version>[a-z]+)\.dat$", version_type="text")
    longest = "a" * MAX_VERSION_TEXT
    assert derive(f"x_{longest}.dat", [text]).version_text == longest


# --- DEP-06: rule validation ---------------------------------------------------------

_OK = r"_v(?P<major>\d+)\.(?P<minor>\d+)\.(?P<ext>[^./]+)$"


@pytest.mark.req("DEP-06")
@pytest.mark.parametrize(
    ("kwargs", "field", "message"),
    [
        ({"extraction_pattern": "("}, "extraction_pattern", "invalid regex"),
        ({"extraction_pattern": ""}, "extraction_pattern", "at least 1"),
        ({"pattern": r"(?P<version>\d)"}, "pattern", "Extra inputs"),
        (
            {"extraction_pattern": r"(?P<version>\d)", "latest_by": "minor"},
            "latest_by",
            "Extra inputs",
        ),
        ({"extraction_pattern": "x", "version_type": "semver"}, "version_type", "'number'"),
        (
            {"extraction_pattern": _OK, "version_text": "{major}"},
            "version_text",
            "only allowed with version_type: text",
        ),
        (
            {"extraction_pattern": _OK, "version_type": "text", "version_text": "{patch}"},
            "version_text",
            "{patch}, which is not a named group",
        ),
        (
            {
                "extraction_pattern": r"(?P<date>\d{8})_(?P<version>\d+)",
                "version_type": "text",
                "version_text": "{date}",
            },
            "version_text",
            "'date' group",
        ),
        (
            {"extraction_pattern": _OK, "version_type": "text", "version_text": "{}"},
            "version_text",
            "must be the name of a group",
        ),
        (
            {"extraction_pattern": _OK, "version_type": "text", "version_text": "{0}"},
            "version_text",
            "must be the name of a group",
        ),
        (
            {"extraction_pattern": _OK, "version_type": "text", "version_text": "{major.real}"},
            "version_text",
            "must be the name of a group",
        ),
        (
            {"extraction_pattern": _OK, "version_type": "text", "version_text": "{major[0]}"},
            "version_text",
            "must be the name of a group",
        ),
        (
            {"extraction_pattern": _OK, "version_type": "text", "version_text": "{major:03d}"},
            "version_text",
            "does not format",
        ),
        (
            {"extraction_pattern": _OK, "version_type": "text", "version_text": "{major"},
            "version_text",
            "invalid format template",
        ),
        (
            {"extraction_pattern": _OK, "version_type": "text", "version_text": ""},
            "version_text",
            "at least 1",
        ),
        (
            {"extraction_pattern": _OK, "ignore_in_series": ["patch"]},
            "ignore_in_series",
            "'patch', which is not a named group",
        ),
        (
            {
                "extraction_pattern": r"(?P<date>\d{8})_(?P<version>\d+)",
                "ignore_in_series": ["date"],
            },
            "ignore_in_series",
            "'date' group",
        ),
        ({"paths": [], "extraction_pattern": "x"}, "paths", "at least 1"),
        ({"paths": [" "], "extraction_pattern": "x"}, "paths", "must not be empty"),
        ({"paths": None, "extraction_pattern": "x"}, "paths", "valid list"),
        ({"paths": "MISSING", "extraction_pattern": "x"}, "paths", "required"),
        ({"paths": ["**"]}, "extraction_pattern", "required"),
    ],
)
def test_invalid_rules(kwargs: dict[str, Any], field: str, message: str) -> None:
    data = {"paths": ["**"], **kwargs}
    if data["paths"] == "MISSING":
        del data["paths"]
    with pytest.raises(ValidationError) as err:
        VersionRule.model_validate(data)
    errors = {e["loc"]: e["msg"] for e in err.value.errors()}
    assert message in errors[(field,)], errors


@pytest.mark.req("DEP-06")
def test_valid_rules() -> None:
    rule = VersionRule.model_validate(
        {
            "paths": ["cal/**", "x/*"],
            "version_type": "text",
            "extraction_pattern": _OK,
            "version_text": "{major:0>3}.{minor:0>4}{major}",
            "ignore_in_series": ["ext"],
        }
    )
    assert rule.version_groups == ("major", "minor")
    assert _rule(r"_(?P<version>\d+)").version_groups == ("version",)
    assert _rule(r"\d+").version_groups == ()
    assert _rule(r"\d+").version_type == "number"
    # A text rule without a template is valid, and a group-free template is too.
    assert _rule(r"\d+", version_type="text").version_text is None
    assert _rule(r"\d+", version_type="text", version_text="v1").version_groups == ()


# --- globs ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "pattern", "expected"),
    [
        ("a/b/c.cdf", "**/*.cdf", True),
        ("c.cdf", "**/*.cdf", True),
        ("a/b/c.cdf", "a/*.cdf", False),
        ("a/c.cdf", "a/?.cdf", True),
        ("a/b/c.cdf", "a/**", True),
        ("a.b", "a.b", True),
        ("axb", "a.b", False),
    ],
)
def test_glob_match(path: str, pattern: str, expected: bool) -> None:
    assert glob_match(path, pattern) is expected


def test_literal_prefix() -> None:
    assert literal_prefix("sci/2025/*.cdf") == "sci/2025/"
    assert literal_prefix("exact.txt") == "exact.txt"

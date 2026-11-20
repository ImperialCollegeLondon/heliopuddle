"""Versioning: version extraction, overrides and series keys."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from heliopuddle.config.models import DEFAULT_VERSION_RULES, ProjectConfig, VersionRule
from heliopuddle.versioning import (
    VersionInfo,
    apply_override,
    derive,
    display_series_key,
    literal,
)
from tests.unit._versioning_helpers import (
    _rule,
)

DEFAULT = list(DEFAULT_VERSION_RULES)


MAJOR_MINOR = r"_v(?P<major>\d{1,3})\.(?P<minor>\d{1,4})\.(?P<ext>[^./]+)$"


PADDED = "{major:0>3}.{minor:0>4}"


def _view(info: VersionInfo) -> tuple[str, int | None, str | None]:
    return display_series_key(info.series_key), info.version_number, info.version_text


# --- API-02: number and text versions -----------------------------------------------


@pytest.mark.req("API-02")
@pytest.mark.parametrize(
    ("path", "key", "number"),
    [
        ("file_v8.csv", "file_v{v}.csv", 8),
        ("file_V09.csv", "file_V{v}.csv", 9),
        ("file_v10.csv", "file_v{v}.csv", 10),
        ("sci/a_20250101_v002.cdf", "sci/a_20250101_v{v}.cdf", 2),
        ("sci/a_20250101_v002.csv", "sci/a_20250101_v{v}.csv", 2),
        ("x_v99999.csv", "x_v{v}.csv", 99999),
        # Not versions under the default rule: dates, frame numbers, bare numbers.
        ("a_20250101.csv", "a_20250101.csv", None),
        ("img_001.png", "img_001.png", None),
        ("file_6.csv", "file_6.csv", None),
        # Six digits is more than the default rule takes.
        ("x_v123456.csv", "x_v123456.csv", None),
        ("notes/readme.txt", "notes/readme.txt", None),
    ],
)
def test_default_rule(path: str, key: str, number: int | None) -> None:
    info = derive(path, DEFAULT)
    assert _view(info) == (key, number, None)
    assert info.series_key == ("v:" if number is not None else "p:") + key
    assert info.is_versioned == (number is not None)
    assert info.version_type == ("number" if number is not None else None)


@pytest.mark.req("API-02")
@pytest.mark.parametrize(
    ("rule", "path", "expected"),
    [
        # number from the version group / from the whole match
        (_rule(r"_(?P<version>\d{1,10})\.[^./]+$"), "x/file_12.csv", ("x/file_{v}.csv", 12, None)),
        (_rule(r"[0-9]+(?=\.[^./]+$)"), "x/file_12.csv", ("x/file_{v}.csv", 12, None)),
        (_rule(r"_(?P<version>\d{2})\.[^./]+$"), "spice/k_07.bsp", ("spice/k_{v}.bsp", 7, None)),
        # text with a padding template
        (
            _rule(MAJOR_MINOR, version_type="text", version_text=PADDED),
            "file_v001.1234.csv",
            ("file_v{major}.{minor}.csv", None, "001.1234"),
        ),
        (
            _rule(MAJOR_MINOR, version_type="text", version_text=PADDED),
            "file_v2.12.csv",
            ("file_v{major}.{minor}.csv", None, "002.0012"),
        ),
        (
            _rule(MAJOR_MINOR, version_type="text", version_text=PADDED),
            "file_v0.0001.csv",
            ("file_v{major}.{minor}.csv", None, "000.0001"),
        ),
        (
            _rule(MAJOR_MINOR, version_type="text", version_text=PADDED),
            "cal/x_v1.4.json",
            ("cal/x_v{major}.{minor}.json", None, "001.0004"),
        ),
        # text without a template: the version group, else the whole match
        (
            _rule(r"_(?P<version>[a-z]+\d*)\.[^./]+$", version_type="text"),
            "doc_rc2.txt",
            ("doc_{v}.txt", None, "rc2"),
        ),
        (
            _rule(r"r[0-9]+[a-z]?(?=\.[^./]+$)", version_type="text"),
            "doc_r12b.txt",
            ("doc_{v}.txt", None, "r12b"),
        ),
        # an optional group that did not take part formats as ""
        (
            _rule(
                r"_v(?P<major>\d+)(?:-(?P<tag>[a-z]+))?\.[^./]+$",
                version_type="text",
                version_text="{major:0>2}{tag}",
            ),
            "x_v3.dat",
            ("x_v{major}.dat", None, "03"),
        ),
        (
            _rule(
                r"_v(?P<major>\d+)(?:-(?P<tag>[a-z]+))?\.[^./]+$",
                version_type="text",
                version_text="{major:0>2}{tag}",
            ),
            "x_v3-rc.dat",
            ("x_v{major}-{tag}.dat", None, "03rc"),
        ),
        # ignore_in_series: ext joins extensions into one series
        (
            _rule(MAJOR_MINOR, version_type="text", version_text=PADDED, ignore_in_series=["ext"]),
            "cal/x_v1.4.csv",
            ("cal/x_v{major}.{minor}.{ext}", None, "001.0004"),
        ),
    ],
)
def test_rule_extraction(
    rule: VersionRule, path: str, expected: tuple[str, int | None, str | None]
) -> None:
    info = derive(path, [rule])
    assert _view(info) == expected
    assert info.series_key.startswith("v:")


@pytest.mark.req("API-02")
def test_extension_splits_the_series_unless_ignored() -> None:
    split = _rule(MAJOR_MINOR, version_type="text", version_text=PADDED)
    joined = _rule(MAJOR_MINOR, version_type="text", version_text=PADDED, ignore_in_series=["ext"])
    assert derive("x_v1.4.csv", [split]).series_key != derive("x_v1.4.json", [split]).series_key
    assert derive("x_v1.4.csv", [joined]).series_key == derive("x_v1.4.json", [joined]).series_key
    # The default rule keeps the extension in the key as well.
    assert derive("a_v2.cdf", DEFAULT).series_key != derive("a_v2.csv", DEFAULT).series_key


@pytest.mark.req("API-02")
def test_minor_only_ordering_with_major_ignored_in_series() -> None:
    rule = _rule(r"_v(?P<major>\d+)\.(?P<version>\d+)\.[^./]+$", ignore_in_series=["major"])
    old, new = derive("cal/x_v1.4.json", [rule]), derive("cal/x_v2.5.json", [rule])
    assert old.series_key == new.series_key == "v:cal/x_v{major}.{v}.json"
    assert (old.version_number, new.version_number) == (4, 5)


@pytest.mark.req("API-02")
def test_date_group_gives_content_date_and_stays_literal_in_the_key() -> None:
    rule = _rule(r"_(?P<date>\d{8})_r(?P<version>\d+)\.[^./]+$", paths=["dated/*"])
    info = derive("dated/obs_20250102_r3.csv", [rule])
    assert _view(info) == ("dated/obs_20250102_r{v}.csv", 3, None)
    assert info.content_date == datetime(2025, 1, 2, tzinfo=UTC)
    other_day = derive("dated/obs_20250103_r3.csv", [rule])
    assert other_day.series_key != info.series_key
    # An unparseable date is ignored; the version still counts.
    bad = derive("dated/obs_20251399_r3.csv", [rule])
    assert (bad.content_date, bad.version_number) == (None, 3)
    dashed = _rule(r"_(?P<date>\d{4}-\d{2}-\d{2})_v(?P<version>\d+)\.[^./]+$")
    assert derive("a_2025-03-04_v1.csv", [dashed]).content_date == datetime(2025, 3, 4, tzinfo=UTC)
    assert derive("a_v1.csv", DEFAULT).content_date is None


@pytest.mark.req("API-02")
@pytest.mark.parametrize(
    ("rule", "path", "key"),
    [
        # Whole-match version that contains the date.
        (
            _rule(r"(?P<date>\d{8})_v\d+", version_type="text"),
            "x_20250101_v1.csv",
            "x_20250101{v}.csv",
        ),
        # A version group around the date.
        (
            _rule(r"_(?P<version>(?P<date>\d{8})\d{2})\.", version_type="text"),
            "x_2025010107.csv",
            "x_20250101{v}.csv",
        ),
        # The date in the middle of the whole match.
        (
            _rule(r"v(?P<date>\d{8})_\d+", version_type="text"),
            "xv20250101_3.csv",
            "x{v}20250101{v}.csv",
        ),
    ],
)
def test_a_date_inside_a_version_part_stays_literal_in_the_key(
    rule: VersionRule, path: str, key: str
) -> None:
    info = derive(path, [rule])
    assert display_series_key(info.series_key) == key
    assert info.content_date is not None
    # Another date is another series, so no rule treats one date's file as an
    # old version of another's.
    other = derive(path.replace("20250101", "20250102"), [rule])
    assert other.series_key != info.series_key


@pytest.mark.req("API-02")
def test_first_matching_rule_wins_and_a_pattern_miss_falls_through() -> None:
    rules = [
        _rule(r"_(?P<version>\d{2})\.[^./]+$", paths=["spice/**"]),
        _rule(r"_k(?P<version>\d+)\.[^./]+$", paths=["spice/**"]),
        _rule(r"_(?P<version>\d+)\.[^./]+$", version_type="text"),
    ]
    assert _view(derive("spice/a_07.bsp", rules)) == ("spice/a_{v}.bsp", 7, None)
    # paths match rule 0 but its pattern does not: rule 1 is tried
    assert _view(derive("spice/a_k7.bsp", rules)) == ("spice/a_k{v}.bsp", 7, None)
    # rules 0 and 1 miss: rule 2 (text) applies
    assert _view(derive("spice/a_7.bsp", rules)) == ("spice/a_{v}.bsp", None, "7")
    # no rule matches
    assert derive("spice/a.bsp", rules) == literal("spice/a.bsp")


@pytest.mark.req("API-02")
def test_empty_rules_mean_everything_is_unversioned() -> None:
    for path in ("file_v8.csv", "x_v1.4.json", "readme.txt"):
        info = derive(path, [])
        assert info == VersionInfo(series_key="p:" + path)
        assert not info.is_versioned and info.version_type is None


@pytest.mark.req("API-02")
def test_project_without_versioning_gets_the_default_rule() -> None:
    base: dict[str, Any] = {
        "project": "p",
        "storage_locations": [{"name": "s", "url": "file:///x"}],
    }
    cfg = ProjectConfig.model_validate(base)
    assert cfg.versioning == list(DEFAULT_VERSION_RULES)
    [rule] = cfg.versioning
    assert (rule.paths, rule.version_type, rule.extraction_pattern) == (
        ["**"],
        "number",
        r"_[vV](?P<version>\d{1,5})\.[^./]+$",
    )
    assert ProjectConfig.model_validate({**base, "versioning": []}).versioning == []
    custom = ProjectConfig.model_validate(
        {**base, "versioning": [{"paths": ["a/**"], "extraction_pattern": "x"}]}
    )
    assert len(custom.versioning) == 1  # replaces the default, nothing appended


@pytest.mark.req("API-02")
def test_padded_text_versions_sort_as_strings() -> None:
    rule = _rule(MAJOR_MINOR, version_type="text", version_text=PADDED)
    names = ["x_v2.12.csv", "x_v10.1.csv", "x_v1.9.csv", "x_v1.10.csv", "x_v0.0001.csv"]
    infos = [derive(n, [rule]) for n in names]
    assert len({i.series_key for i in infos}) == 1
    texts = sorted((i.version_text or "" for i in infos), key=lambda t: t.encode())
    assert texts == ["000.0001", "001.0009", "001.0010", "002.0012", "010.0001"]


@pytest.mark.req("API-02")
@pytest.mark.parametrize(
    ("number", "text", "expected"),
    [
        (42, None, (42, None)),
        (0, None, (0, None)),
        (None, "rc1", (None, "rc1")),
        (None, None, (8, None)),
    ],
)
def test_apply_override(
    number: int | None, text: str | None, expected: tuple[int | None, str | None]
) -> None:
    info = derive("file_v8.csv", DEFAULT)
    out = apply_override(info, number, text)
    assert (out.version_number, out.version_text) == expected
    assert out.series_key == info.series_key == "v:file_v{v}.csv"


@pytest.mark.req("API-02")
def test_apply_override_on_unversioned_path_keeps_its_literal_key() -> None:
    out = apply_override(literal("readme.txt"), None, "a")
    assert (out.series_key, out.version_text, out.version_type) == ("p:readme.txt", "a", "text")
    assert apply_override(literal("readme.txt"), None, None) == literal("readme.txt")


@pytest.mark.req("API-02")
def test_apply_override_rejects_both() -> None:
    with pytest.raises(ValueError, match="not both"):
        apply_override(literal("a"), 1, "1")


# --- API-13: series key namespaces --------------------------------------------------


@pytest.mark.req("API-13")
def test_generated_and_literal_keys_never_collide() -> None:
    generated = derive("x_v1.dat", DEFAULT)
    plain = derive("x_v{v}.dat", DEFAULT)
    assert not plain.is_versioned
    assert generated.series_key == "v:x_v{v}.dat"
    assert plain.series_key == "p:x_v{v}.dat"
    assert generated.series_key != plain.series_key
    assert display_series_key(generated.series_key) == display_series_key(plain.series_key)


@pytest.mark.req("API-13")
@pytest.mark.parametrize(
    ("stored", "shown"),
    [("v:a/x_v{v}.dat", "a/x_v{v}.dat"), ("p:a/x.dat", "a/x.dat"), ("legacy", "legacy")],
)
def test_display_series_key(stored: str, shown: str) -> None:
    assert display_series_key(stored) == shown

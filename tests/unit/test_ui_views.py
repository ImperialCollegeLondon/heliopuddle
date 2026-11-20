"""Pure presentation helpers of the UI: formatting, health, and the rules diagram layout."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise
from types import SimpleNamespace
from typing import Any

import pytest

from heliopuddle.config.models import AgeCriteria, AgeRange
from heliopuddle.server.schemas import FileState
from heliopuddle.server.ui.pages.files import paging as ui_paging
from heliopuddle.server.ui.pages.overview import heatmap as ui_heatmap
from heliopuddle.server.ui.pages.rules import flow as ui_flow
from heliopuddle.server.ui.shared import fileinfo, rules_text
from heliopuddle.server.ui.shared import format as ui_format
from tests.unit._ui_helpers import LATER, NOW, Factory, file_with, make_file, sl_read, ui_client
from tests.unit.test_ui_rules import rule


def sl(name: str, active: bool = True) -> Any:
    return SimpleNamespace(name=name, active=active, files=3, bytes=2048)


def r(name: str, action: str, src: str, dst: str | None, enabled: bool = True) -> Any:
    return rule().model_copy(
        update={
            "name": name,
            "action": action,
            "source": src,
            "destination": dst,
            "effective_enabled": enabled,
        }
    )


# --- formatting ---------------------------------------------------------------------------


@pytest.mark.req("UI-01")
@pytest.mark.parametrize(
    ("delta", "text"),
    [
        (timedelta(seconds=5), "just now"),
        (timedelta(minutes=12), "12 min ago"),
        (timedelta(hours=3), "3 h ago"),
        (timedelta(days=1), "1 day ago"),
        (timedelta(days=6), "6 days ago"),
        (timedelta(days=90), "2 months ago"),
        (timedelta(days=800), "2 years ago"),
        (-timedelta(days=2), "in 2 days"),
    ],
)
def test_relative_time(delta: timedelta, text: str) -> None:
    assert ui_format.relative_time(NOW - delta, now=NOW) == text


@pytest.mark.req("UI-01")
def test_small_formatters() -> None:
    assert ui_format.relative_time(None) == ""
    assert ui_format.relative_time(datetime(2026, 1, 2), now=NOW) == "3 h ago"  # naive is UTC
    assert ui_format.number(48213) == "48,213" and ui_format.number(None) == ""
    assert ui_format.human_size(None) == ""
    assert ui_format.format_date(datetime(2026, 1, 2, tzinfo=UTC)) == "2026-01-02"
    assert ui_format.format_date(NOW) == "2026-01-02 03:04"
    assert ui_format.format_date(None) == ""
    assert ui_format.format_datetime(None) == ""
    assert ui_format.split_path("a/b/c.txt") == ("a/b/", "c.txt")
    assert ui_format.split_path("c.txt") == ("", "c.txt")
    assert ui_format.initials("ann.lee") == "AL" and ui_format.initials("bob") == "BO"
    assert ui_format.initials("") == "?"
    assert ui_format.scheme("") == "" and ui_format.scheme("/plain/path") == "FILE"
    assert ui_format.format_duration(timedelta(days=30)) == "30d"
    assert ui_format.format_duration(timedelta(hours=12)) == "12h"
    assert ui_format.format_duration(timedelta(minutes=45)) == "45m"
    assert ui_format.format_duration(timedelta(seconds=61)) == "61s"


@pytest.mark.req("UI-02")
def test_metadata_value_formatting() -> None:
    assert ui_format.metadata_value("plain") == "plain"
    assert ui_format.metadata_value({"b": 1, "a": None}) == '{\n  "a": null,\n  "b": 1\n}'
    assert ui_format.metadata_value(NOW) == '"2026-01-02 03:04:05+00:00"'


@pytest.mark.req("UI-12")
@pytest.mark.parametrize(
    ("states", "deleted", "expected"),
    [
        ([FileState.AVAILABLE, FileState.MISSING], False, ("1 copy missing", "missing")),
        ([FileState.UNKNOWN, FileState.UNKNOWN], False, ("2 copies unknown", "unknown")),
        ([FileState.AVAILABLE, FileState.PENDING], False, ("in progress", "pending")),
        ([FileState.AVAILABLE], False, ("all copies available", "available")),
        ([FileState.DELETED], False, ("no copy", "none")),
        ([], False, ("no copy", "none")),
        ([FileState.AVAILABLE], True, ("deleted", "none")),
    ],
)
def test_file_health(states: list[FileState], deleted: bool, expected: tuple[str, str]) -> None:
    f = file_with(1, *((f"s{i}", s) for i, s in enumerate(states)))
    if deleted:
        f = f.model_copy(update={"deleted_at": LATER})
    assert fileinfo.file_health(f) == expected


@pytest.mark.req("UI-12")
def test_downloadable_needs_a_readable_copy_of_a_live_file() -> None:
    assert fileinfo.downloadable(make_file(1))
    assert fileinfo.downloadable(make_file(1, state=FileState.OVERWRITING))
    assert not fileinfo.downloadable(make_file(1, state=FileState.PENDING))
    assert not fileinfo.downloadable(make_file(1, deleted_at=LATER))


@pytest.mark.req("UI-04")
def test_rule_summaries() -> None:
    copy = rule()
    assert rules_text.rule_route(copy) == "copy primary → secondary"
    assert rules_text.rule_match_summary(copy) == "data/** · not data/private/**"
    trim = rule().model_copy(update={"action": "delete", "destination": None})
    trim.match.is_latest = False
    trim.match.age = AgeCriteria(
        modified=AgeRange(older_than=timedelta(days=30)),
        arrived=AgeRange(older_than=timedelta(days=3)),
    )
    trim.match.exclude = []
    assert rules_text.rule_route(trim) == "delete from primary"
    assert rules_text.rule_match_summary(trim) == (
        "data/** · non-latest · modified older than 30d · arrived older than 3d"
    )
    trim.match.is_latest = True
    assert "latest" in rules_text.rule_match_summary(trim)


# --- the copy-state heatmap ------------------------------------------------------------------


def summary_row(name: str, no_copy: int = 0, **states: int) -> Any:
    counts = {s: states.get(s.value, 0) for s in FileState}
    return SimpleNamespace(name=name, active=True, states=counts, no_copy=no_copy)


@pytest.mark.req("UI-11")
def test_heatmap_columns_are_the_states_non_zero_in_some_row_in_order() -> None:
    rows = [summary_row("a", available=3, missing=1), summary_row("b", no_copy=2, pending=1)]
    columns, heat = ui_heatmap.heatmap(rows, lambda n, s: f"/f?{n}={s}")
    assert [c.label for c in columns] == ["available", "pending", "missing", "no copy"]
    assert [(c.state, c.count, c.url) for c in heat[1][1]] == [
        ("available", 0, None),
        ("pending", 1, "/f?b=pending"),
        ("missing", 0, None),
        ("none", 2, None),
    ]


@pytest.mark.req("UI-11")
def test_heatmap_with_only_zeros_or_no_rows_has_no_state_columns() -> None:
    assert ui_heatmap.heatmap([summary_row("a")], lambda n, s: "")[0] == []
    assert ui_heatmap.heatmap([], lambda n, s: "") == ([], [])


# --- the rules diagram ------------------------------------------------------------------------


@pytest.mark.req("UI-04")
def test_flow_depths_follow_the_rules_and_survive_cycles() -> None:
    names = ["a", "b", "c", "d"]
    rules = [r("1", "copy", "a", "b"), r("2", "copy", "b", "c"), r("3", "move", "a", "c")]
    assert ui_flow.flow_depths(names, rules) == {"a": 0, "b": 1, "c": 2, "d": 0}
    cycle = [r("1", "copy", "a", "b"), r("2", "copy", "b", "a")]
    depths = ui_flow.flow_depths(["a", "b"], cycle)
    assert all(0 <= d < 2 for d in depths.values())
    assert ui_flow.flow_depths([], []) == {}


@pytest.mark.req("UI-04")
def test_layout_puts_destinations_right_of_their_sources_and_stacks_columns() -> None:
    locations = [sl("ingest"), sl("archive"), sl("offsite"), sl("cold", active=False)]
    rules = [
        r("everything", "copy", "ingest", "archive"),
        r("latest", "copy", "archive", "offsite"),
        r("quicklook", "copy", "archive", "offsite"),
        r("old", "move", "archive", "cold", enabled=False),
        r("trim", "delete", "ingest", None),
    ]
    flow = ui_flow.layout_flow(locations, rules, {"ingest": "FILE"})
    nodes = {n.name: n for n in flow.nodes}
    assert nodes["ingest"].x < nodes["archive"].x < nodes["offsite"].x == nodes["cold"].x
    assert nodes["offsite"].y != nodes["cold"].y
    assert nodes["ingest"].role == "source" and nodes["cold"].role == "inactive"
    assert nodes["archive"].role == "" and nodes["ingest"].kind == "FILE"
    assert [d.name for d in nodes["ingest"].deletes] == ["trim"]
    # one curve per pair of storage locations, one label per rule
    assert len(flow.edges) == 4  # 3 pairs and the delete stub
    labels = {label.rule: label for label in flow.labels}
    assert set(labels) == {"everything", "latest", "quicklook", "old", "trim"}
    assert labels["latest"].x == labels["quicklook"].x
    assert abs(labels["latest"].y - labels["quicklook"].y) == 30
    assert labels["old"].text == "old · move · off" and not labels["old"].enabled
    assert labels["trim"].y > nodes["ingest"].y + ui_flow.NODE_H
    for node in flow.nodes:
        assert node.x + ui_flow.NODE_W <= flow.width
        assert node.y + ui_flow.NODE_H <= flow.height


@pytest.mark.req("UI-04")
def test_layout_routes_backward_rules_under_the_boxes() -> None:
    flow = ui_flow.layout_flow(
        [sl("a"), sl("b")], [r("there", "copy", "a", "b"), r("back", "copy", "b", "a")]
    )
    start = flow.nodes[1].x + ui_flow.NODE_W // 2 + 20
    back = [e for e in flow.edges if e.path.startswith(f"M{start} ")]
    assert back, flow.edges
    lowest = max(n.y + ui_flow.NODE_H for n in flow.nodes)
    label = next(label for label in flow.labels if label.rule == "back")
    assert label.y > lowest
    assert flow.height > label.y


@pytest.mark.req("UI-04")
def test_layout_scales_to_many_storage_locations() -> None:
    locations = [sl(f"s{i}") for i in range(12)]
    rules = [r(f"r{i}", "copy", f"s{i}", f"s{i + 1}") for i in range(0, 11, 2)]
    flow = ui_flow.layout_flow(locations, rules)
    assert len({n.x for n in flow.nodes}) == 2
    ys = sorted(n.y for n in flow.nodes if n.x == flow.nodes[0].x)
    assert all(b - a >= ui_flow.NODE_H for a, b in pairwise(ys))
    empty = ui_flow.layout_flow([], [])
    assert empty.nodes == [] and empty.width > 0


@pytest.mark.req("UI-04")
def test_rules_page_draws_the_diagram_without_javascript() -> None:
    factory = Factory(
        storage_locations=[sl_read("primary"), sl_read("secondary")],
        rules=[rule(), r("trim", "delete", "primary", None, enabled=False)],
    )
    html = ui_client(factory).get("/projects/demo/rules").text
    assert '<svg class="flow-svg"' in html
    assert 'class="edge edge-copy"' in html and 'marker-end="url(#arrow-copy)"' in html
    assert 'class="edge edge-off"' in html  # the disabled delete rule
    assert 'class="flow-label flow-copy" href="#rule-archive"' in html
    assert "trim · delete · off" in html
    assert 'href="/projects/demo/storage/secondary"' in html
    assert '<tr id="rule-archive" data-rule="archive">' in html
    empty = ui_client(Factory()).get("/projects/demo/rules").text
    assert "No rules yet" in empty and "No configured rules" in empty


@pytest.mark.req("UI-04")
def test_rule_full_summary_is_complete() -> None:
    trim = rule().model_copy(update={"action": "delete", "destination": None})
    trim.match.is_latest = False
    trim.match.age = AgeCriteria(
        modified=AgeRange(older_than=timedelta(days=30)),
        arrived=AgeRange(older_than=timedelta(days=3)),
        content_date=AgeRange(
            older_than=datetime(2025, 6, 1, tzinfo=UTC),
            younger_than=datetime(2024, 1, 1, tzinfo=UTC),
        ),
    )
    trim.options.batch.max_files = 10
    selects, options = rules_text.rule_full_summary(trim)
    assert selects[0] == "Selects"
    assert selects[1] == (
        "data/** · excluding data/private/** · non-latest versions only"
        " · modified older than 30d, content date between 2024-01-01 and 2025-06-01,"
        " arrived older than 3d"
    )
    assert options[0] == "Options"
    assert options[1].startswith("no overwrite · space check (+2GB)")
    assert options[1].endswith("batch of 10 files / 1GB / 30m")  # nothing truncated


@pytest.mark.req("UI-04")
@pytest.mark.parametrize(
    ("bounds", "text"),
    [
        (AgeRange(older_than=timedelta(hours=12)), "created older than 12h"),
        (AgeRange(younger_than=timedelta(days=365)), "created younger than 365d"),
        (
            AgeRange(older_than=timedelta(days=1), younger_than=timedelta(days=7)),
            "created older than 1d and younger than 7d",
        ),
        (
            AgeRange(
                older_than=timedelta(days=1), younger_than=datetime(2025, 1, 1, 6, 30, tzinfo=UTC)
            ),
            "created older than 1d and younger than 2025-01-01 06:30",
        ),
        (
            AgeRange(older_than=datetime(2025, 1, 1, tzinfo=UTC)),
            "created older than 2025-01-01",
        ),
    ],
)
def test_age_descriptions(bounds: AgeRange, text: str) -> None:
    assert rules_text.age_descriptions(AgeCriteria(created=bounds)) == [text]
    assert rules_text.age_descriptions(AgeCriteria()) == []


@pytest.mark.req("UI-12")
def test_choose_columns_can_be_empty() -> None:
    sls = [sl_read("a"), sl_read("b")]
    assert ui_paging.choose_columns(sls, ui_paging.NO_COLUMNS) == []
    assert ui_paging.choose_columns(sls, "b") == ["b"]
    assert ui_paging.choose_columns(sls, "nope") == ["a", "b"]

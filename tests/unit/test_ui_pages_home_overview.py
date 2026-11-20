"""The M6 home page and project Overview."""

from __future__ import annotations

import re

import pytest

from heliopuddle.client import HelioPuddleError
from tests.unit._ui_helpers import (
    LATER,
    Factory,
    file_with,
    section,
    sl_read,
    ui_client,
)
from tests.unit._ui_pages_helpers import A, P, fleet, fleet_factory, three_locations
from tests.unit.test_ui_rules import rule


def two_projects() -> Factory:
    files = [
        *fleet(),
        file_with(10, ("ingest", A), project="beta"),
        file_with(11, ("ingest", A), project="beta"),
    ]
    return Factory(projects=["demo", "beta"], files=files, storage_locations=three_locations())


@pytest.mark.req("UI-10")
def test_home_shows_a_card_per_project_with_figures_and_attention_badge() -> None:
    factory = two_projects()
    html = ui_client(factory).get("/").text
    demo = section(html, 'href="/projects/demo">', "</a>")
    assert "5" in section(demo, "Files</span>", "</span></span>")
    assert "1 missing" in demo and "st-missing" in demo
    for sl in ("ingest", "archive", "cold"):
        assert sl in demo
    beta = section(html, 'href="/projects/beta">', "</a>")
    assert "healthy" in beta and "3.0 KiB" not in beta  # 1.5 KiB * (10 + 11)
    assert "31.5 KiB" in beta
    assert sorted(factory.calls("project_summary")) == ["beta", "demo"]
    assert "2 projects · 7 live files" in html
    assert "1 needs attention" in html


@pytest.mark.req("UI-10")
def test_home_lists_needs_attention_across_projects_linking_to_files() -> None:
    html = ui_client(two_projects()).get("/").text
    attention = section(html, 'id="att-heading"')
    assert "1 missing copy on archive" in attention
    assert "1 unknown copy on ingest" in attention
    assert (
        'href="/projects/demo/files?view=attention&amp;storage_location=archive&amp;state=missing"'
        in attention
    )
    assert (
        'href="/projects/demo/files?view=attention&amp;storage_location=ingest&amp;state=unknown"'
        in attention
    )
    healthy = ui_client(Factory(projects=["a", "b"])).get("/").text
    assert "Nothing needs attention" in healthy


@pytest.mark.req("UI-10")
def test_home_keeps_going_when_one_summary_fails() -> None:
    factory = two_projects()
    factory.summary_error = HelioPuddleError(503, "busy")
    html = ui_client(factory).get("/").text
    assert "summary unavailable" in html
    assert 'href="/projects/beta"' in html


@pytest.mark.req("UI-10")
def test_home_passes_on_a_401_from_a_summary() -> None:
    factory = two_projects()
    factory.summary_error = HelioPuddleError(401, "expired")
    assert ui_client(factory).get("/").status_code == 401


@pytest.mark.req("UI-11")
def test_overview_shows_key_figures() -> None:
    html = ui_client(fleet_factory()).get("/projects/demo").text
    kpis = section(html, 'aria-label="Key figures"')
    figures = dict(
        re.findall(r'<span class="lbl">([^<]+)</span><span class="kpi-value">([^<]+)</span>', kpis)
    )
    assert figures == {
        "Live files": "5",
        "Latest files": "4",
        "Stored": "22.5 KiB",
        "Needs attention": "2",
        "In progress": "1",
    }
    assert 'href="/projects/demo/files?view=attention"' in kpis


@pytest.mark.req("UI-11")
def test_overview_heatmap_has_a_row_per_storage_location_and_linked_cells() -> None:
    html = ui_client(fleet_factory()).get("/projects/demo").text
    heat = section(html, '<table class="heatmap">', "</table>")
    head = section(heat, "<thead>", "</thead>")
    columns = re.findall(r'<th scope="col">([^<]+)</th>', head)
    # overwriting, deleting and deleted are zero on every storage location: no column
    assert columns == ["available", "pending", "unknown", "missing", "no copy"]
    rows = heat.split("<tbody>")[1].split("<tr")[1:]
    assert [re.search(r'storage/(\w+)"', r).group(1) for r in rows] == [  # type: ignore[union-attr]
        "ingest",
        "archive",
        "cold",
    ]
    ingest = rows[0]
    assert 'href="/projects/demo/files?storage_location=ingest&amp;state=available"' in ingest
    assert ">4</a>" in ingest
    assert 'href="/projects/demo/files?storage_location=ingest&amp;state=unknown"' in ingest
    assert "state=missing" not in ingest  # zero cells are not links
    archive = rows[1]
    assert 'href="/projects/demo/files?storage_location=archive&amp;state=pending"' in archive
    assert '<span class="hm-cell st-none">1</span>' in archive  # file 5 has no copy here
    assert "inactive" in rows[2]
    assert all(r.count("<td>") == len(columns) for r in rows)


@pytest.mark.req("UI-11")
def test_overview_heatmap_leaves_out_no_copy_when_every_file_has_a_copy_everywhere() -> None:
    files = [
        file_with(1, ("ingest", A), ("archive", A)),
        file_with(2, ("ingest", A), ("archive", P)),
    ]
    factory = Factory(files=files, storage_locations=[sl_read("ingest"), sl_read("archive")])
    heat = section(
        ui_client(factory).get("/projects/demo").text, '<table class="heatmap">', "</table>"
    )
    assert re.findall(r'<th scope="col">([^<]+)</th>', heat) == ["available", "pending"]
    assert "st-none" not in heat


@pytest.mark.req("UI-11")
def test_overview_needs_attention_and_rules_summary() -> None:
    finished = rule().model_copy(update={"last_success_at": LATER})
    html = ui_client(fleet_factory(rules=[finished])).get("/projects/demo").text
    attention = section(html, 'id="att-heading"')
    assert "1 missing copy on archive" in attention and "Review" in attention
    assert "1 pending copy on archive" in attention and "View" in attention
    assert "view=progress&amp;storage_location=archive&amp;state=pending" in attention
    rules = section(html, 'id="rules-heading"')
    assert 'href="/projects/demo/rules#rule-archive"' in rules
    assert "copy primary → archive" not in rules and "copy primary → secondary" in rules
    assert "0 2 * * *" in rules and "enabled" in rules
    assert 'title="2026-02-03 04:05:06"' in rules
    assert 'href="/projects/demo/rules">All rules' in rules


@pytest.mark.req("UI-11")
def test_overview_recently_added_files() -> None:
    factory = fleet_factory()
    html = ui_client(factory).get("/projects/demo").text
    recent = section(html, 'id="recent-heading"')
    assert 'href="/projects/demo/files?file=1"' in recent
    assert "file-1.csv" in recent


@pytest.mark.req("UI-11", "UI-07", "UI-08")
def test_overview_actions_menu_has_reconcile_and_index_per_active_storage_location() -> None:
    html = ui_client(fleet_factory(rules=[rule()])).get("/projects/demo").text
    menu = section(html, '<details class="menu actions">', "</details>")
    assert "Reconcile now" in menu and 'name="verify"' in menu
    assert 'action="/projects/demo/storage/ingest/index"' in menu
    assert 'action="/projects/demo/storage/archive/index"' in menu
    assert "/storage/cold/index" not in menu  # inactive
    assert menu.count('value="full"') == 2 and menu.count('value="incremental"') == 2


@pytest.mark.req("UI-11")
def test_overview_without_storage_locations_or_files() -> None:
    html = ui_client(Factory(storage_locations=[])).get("/projects/demo").text
    assert "No storage locations." in html
    assert "No files yet." in html
    assert "No active storage locations." in html

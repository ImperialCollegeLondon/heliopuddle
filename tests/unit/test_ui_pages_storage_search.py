"""The M6 storage location pages and search."""

from __future__ import annotations

import re

import pytest

from tests.unit._ui_helpers import (
    Factory,
    make_file,
    section,
    sl_read,
    ui_client,
)
from tests.unit._ui_pages_helpers import fleet, fleet_factory, three_locations
from tests.unit.test_ui_rules import rule


def search_factory() -> Factory:
    files = [
        make_file(1, path="raw/obs_20261105.dat", name="obs_20261105.dat"),
        make_file(2, path="raw/2026/obs_20261106.dat", name="obs_20261106.dat"),
        make_file(3, path="products/obs_20261105.json", name="obs_20261105.json", project="beta"),
        make_file(4, path="other/thing.dat", name="thing.dat"),
    ]
    return Factory(projects=["demo", "beta"], files=files)


@pytest.mark.req("UI-13")
def test_storage_list_shows_each_storage_location() -> None:
    sls = [*three_locations(), sl_read("hidden", url="")]
    html = (
        ui_client(Factory(files=fleet(), storage_locations=sls)).get("/projects/demo/storage").text
    )
    cards = html.split('<article class="card sl-card')[1:]
    assert len(cards) == 4
    assert 'href="/projects/demo/storage/ingest"' in cards[0]
    assert "file:///srv/ingest" in cards[0] and ">FILE<" in cards[0]
    assert "inactive" in cards[2]
    assert "Restricted" in cards[3]
    assert 'class="segbar"' in cards[0]


@pytest.mark.req("UI-13")
def test_storage_location_page_shows_figures_bar_config_and_rules() -> None:
    rules = [
        rule().model_copy(update={"source": "ingest", "destination": "archive"}),
        rule().model_copy(
            update={"name": "trim", "action": "delete", "source": "archive", "destination": None}
        ),
        rule().model_copy(update={"name": "elsewhere", "source": "cold", "destination": "x"}),
    ]
    html = ui_client(fleet_factory(rules=rules)).get("/projects/demo/storage/archive").text
    kpis = section(html, 'aria-label="Key figures"')
    figures = dict(
        re.findall(r'<span class="lbl">([^<]+)</span><span class="kpi-value">([^<]+)</span>', kpis)
    )
    assert figures == {
        "Files": "4",
        "Stored": "15.0 KiB",
        "In progress": "1",
        "Needs attention": "1",
    }
    bar = section(html, '<div class="segbar segbar-lg">', "</div>")
    assert 'href="/projects/demo/files?storage_location=archive&amp;state=available"' in bar
    assert 'href="/projects/demo/files?storage_location=archive&amp;state=missing"' in bar
    assert 'class="seg seg-none"' in bar and "1 no copy" in bar
    config = section(html, 'id="cfg-heading"')
    assert 'Backend</dt><dd class="mono">file' in config
    assert "file:///srv/archive" in config and "active" in config
    touching = section(html, 'id="rl-heading"')
    assert ">IN</span>" in touching and ">OUT</span>" in touching
    assert 'href="/projects/demo/rules#rule-archive"' in touching
    assert 'href="/projects/demo/rules#rule-trim"' in touching
    assert "elsewhere" not in touching
    assert 'href="/projects/demo/files?storage_location=archive">Browse files' in html
    nav = section(html, '<nav class="slnav"', "</nav>")
    assert 'href="/projects/demo/storage/archive" aria-current="page"' in nav


@pytest.mark.req("UI-13")
def test_inactive_storage_location_has_no_index_buttons_and_no_rules_says_so() -> None:
    html = ui_client(fleet_factory()).get("/projects/demo/storage/cold").text
    assert "Index · full" not in html
    assert "No rule reads from or writes to cold." in html


@pytest.mark.req("UI-13")
def test_unknown_storage_location_is_404() -> None:
    resp = ui_client(fleet_factory()).get("/projects/demo/storage/nope")
    assert resp.status_code == 404
    assert "Storage location &#39;nope&#39; not found" in resp.text


@pytest.mark.req("UI-14")
def test_search_page_groups_results_by_project_and_links_to_files() -> None:
    factory = search_factory()
    html = ui_client(factory).get("/search", params={"q": "obs 2026"}).text
    assert factory.calls("search") == [("obs 2026", None, 50, 0)]
    groups = html.split('<section class="card result-group"')[1:]
    assert len(groups) == 2
    assert 'href="/projects/demo"' in groups[0] and "2 matches" in groups[0]
    assert 'href="/projects/demo/files/1"' in groups[0]
    assert 'href="/projects/demo/files/2"' in groups[0]
    assert 'href="/projects/beta/files/3"' in groups[1] and "1 match" in groups[1]
    assert "<mark>obs</mark>" in html
    assert "3 files match" in html
    assert 'href="/search?q=obs+2026&amp;project=beta"' in html  # narrow to one project


@pytest.mark.req("UI-14")
def test_search_within_a_project_links_to_widen() -> None:
    factory = search_factory()
    html = ui_client(factory).get("/search", params={"q": "obs", "project": "demo"}).text
    assert factory.calls("search") == [("obs", ["demo"], 50, 0)]
    assert "beta" not in section(html, "result-group", "</section>")
    assert 'href="/search?q=obs">Search all projects' in html
    assert '<option value="demo" selected>' in html
    header = section(html, '<header class="hp-header">', "</header>")
    assert '<input type="hidden" name="project" value="demo">' in header


@pytest.mark.req("UI-14")
def test_search_pages() -> None:
    files = [make_file(i, path=f"x/obs_{i:03d}.dat") for i in range(1, 121)]
    client = ui_client(Factory(files=files))
    first = client.get("/search", params={"q": "obs"}).text
    assert "1\u201350 of 120" in first
    assert 'rel="next" href="/search?q=obs&amp;offset=50"' in first
    last = client.get("/search", params={"q": "obs", "offset": 100}).text
    assert "101\u2013120 of 120" in last and 'rel="next"' not in last
    assert 'rel="prev" href="/search?q=obs&amp;offset=50"' in last


@pytest.mark.req("UI-14")
def test_search_needs_two_characters_and_says_when_nothing_matches() -> None:
    factory = search_factory()
    client = ui_client(factory)
    short = client.get("/search", params={"q": "o"})
    assert short.status_code == 200 and "at least two characters" in short.text
    empty = client.get("/search").text
    assert "Find files by path" in empty
    assert factory.calls("search") == []
    none = client.get("/search", params={"q": "zzz", "project": "demo"}).text
    assert "No files match “zzz”" in none and 'href="/search?q=zzz"' in none


@pytest.mark.req("UI-14")
def test_search_of_an_unreadable_project_is_404() -> None:
    resp = ui_client(search_factory()).get("/search", params={"q": "obs", "project": "secret"})
    assert resp.status_code == 404


@pytest.mark.req("UI-14")
def test_suggest_fragment_shows_top_results_for_typeahead() -> None:
    factory = search_factory()
    client = ui_client(factory)
    resp = client.get("/search/suggest", params={"q": "obs", "project": "demo"})
    assert resp.status_code == 200
    assert "<html" not in resp.text
    assert factory.calls("search") == [("obs", ["demo"], 8, 0)]
    assert 'class="res" role="option" href="/projects/demo/files/1"' in resp.text
    assert "demo · 2 files" in resp.text
    assert 'href="/search?q=obs&amp;project=demo">See all 2' in resp.text
    assert 'href="/search?q=obs">Search all projects' in resp.text
    assert client.get("/search/suggest", params={"q": "o"}).text == ""
    nothing = client.get("/search/suggest", params={"q": "zzz"}).text
    assert "No files match" in nothing and "See all" not in nothing


@pytest.mark.req("UI-14")
def test_search_without_javascript_is_a_get_form_in_the_header() -> None:
    html = ui_client(search_factory()).get("/").text
    form = section(html, '<form class="search" role="search"', "</form>")
    assert 'method="get" action="/search"' in form
    assert 'data-suggest="/search/suggest"' in form
    assert 'id="hp-suggest"' in form

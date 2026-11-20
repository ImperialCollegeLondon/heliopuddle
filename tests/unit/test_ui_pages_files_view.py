"""The M6 files views, filters, inspector and download."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from heliopuddle.client import HelioPuddleError
from heliopuddle.server.schemas import FileState
from tests.unit._ui_helpers import (
    Factory,
    file_with,
    find_kwargs,
    make_file,
    rows_by_id,
    section,
    series_files,
    sl_read,
    ui_client,
)
from tests.unit._ui_pages_helpers import A, M, P, U, fleet_factory


@pytest.mark.req("UI-12")
def test_view_tabs_show_counts_from_the_summary() -> None:
    html = ui_client(fleet_factory()).get("/projects/demo/files").text
    tabs = section(html, '<nav class="vtabs"', "</nav>")
    found = dict(re.findall(r">([A-Za-z ]+) <span class=\"cnt[^\"]*\">(\d+)</span>", tabs))
    assert found == {
        "All": "5",
        "Latest": "4",
        "Needs attention": "2",
        "In progress": "1",
        "Ignored by rules": "1",
        "Deleted": "1",
    }
    assert 'href="/projects/demo/files" aria-current="page">All' in tabs
    assert 'href="/projects/demo/files?view=attention"' in tabs


@pytest.mark.req("UI-12")
@pytest.mark.parametrize(
    ("view", "kwargs", "ids"),
    [
        ("all", {}, {"1", "2", "3", "4", "5"}),
        ("latest", {"latest_only": True}, {"1", "2", "3", "4"}),
        ("attention", {"state": [U, M]}, {"3", "4"}),
        ("progress", {"state": [P, FileState.OVERWRITING, FileState.DELETING]}, {"2"}),
        ("ignored", {"ignored_from_all_rules": True}, {"4"}),
        ("deleted", {"deleted_only": True}, {"6"}),
    ],
)
def test_views_select_files(view: str, kwargs: dict[str, object], ids: set[str]) -> None:
    factory = fleet_factory()
    html = ui_client(factory).get("/projects/demo/files", params={"view": view}).text
    assert factory.calls("find") == [find_kwargs(**kwargs)]
    assert set(rows_by_id(html)) == ids
    tabs = section(html, '<nav class="vtabs"', "</nav>")
    assert len(re.findall('aria-current="page"', tabs)) == 1


@pytest.mark.req("UI-12")
def test_a_state_filter_replaces_the_view_states_and_switching_view_drops_it() -> None:
    factory = fleet_factory()
    html = (
        ui_client(factory)
        .get(
            "/projects/demo/files",
            params={"view": "attention", "state": "missing", "storage_location": "archive"},
        )
        .text
    )
    assert factory.calls("find") == [find_kwargs(state=[M], storage_location="archive")]
    assert set(rows_by_id(html)) == {"4"}
    tabs = section(html, '<nav class="vtabs"', "</nav>")
    assert 'href="/projects/demo/files?view=progress&amp;storage_location=archive"' in tabs


@pytest.mark.req("UI-12")
def test_storage_location_and_content_date_filters_show_as_chips() -> None:
    factory = fleet_factory()
    params = {
        "storage_location": "ingest",
        "content_from": "2026-03-01",
        "content_to": "2026-03-04",
    }
    html = ui_client(factory).get("/projects/demo/files", params=params).text
    [kwargs] = factory.calls("find")
    assert kwargs["storage_location"] == "ingest"
    assert kwargs["content_date_from"] == datetime(2026, 3, 1, tzinfo=UTC)
    assert kwargs["content_date_to"] == datetime(2026, 3, 4, 23, 59, 59, 999999, tzinfo=UTC)
    assert set(rows_by_id(html)) == {"3"}
    chips = section(html, '<div class="chips"', "<details")
    assert 'Storage location: <span class="chip-value">ingest</span>' in chips
    assert 'Content date: <span class="chip-value">2026-03-01 \u2013 2026-03-04</span>' in chips
    assert '<option value="ingest" selected>' in html


@pytest.mark.req("UI-12")
def test_table_has_a_state_column_per_storage_location() -> None:
    html = ui_client(fleet_factory()).get("/projects/demo/files").text
    head = section(html, "<thead>", "</thead>")
    columns = re.findall(r'<th scope="col" class="col-st" title="Copy state in (\w+)">', head)
    assert columns == ["ingest", "archive"]  # active ones by default
    row = rows_by_id(html)["2"]
    assert row.index("st-available") < row.index("st-pending")
    assert 'aria-label="no copy"' in rows_by_id(html)["5"]


@pytest.mark.req("UI-12")
def test_at_most_six_columns_and_the_chooser_picks_others() -> None:
    sls = [sl_read(f"s{i}") for i in range(1, 9)]
    files = [file_with(1, *((f"s{i}", A) for i in range(1, 9)))]
    client = ui_client(Factory(files=files, storage_locations=sls))
    default = client.get("/projects/demo/files").text
    head = section(default, "<thead>", "</thead>")
    assert re.findall(r'title="Copy state in (\w+)"', head) == [f"s{i}" for i in range(1, 7)]
    menu = section(default, '<details class="menu columns-menu">', "</details>")
    assert menu.count('name="col"') == 8
    assert 'name="col" value="s7">' in menu and 'name="col" value="s1" checked>' in menu
    picked = client.get("/projects/demo/files", params={"columns": "s8,s7,nope"}).text
    head = section(picked, "<thead>", "</thead>")
    assert re.findall(r'title="Copy state in (\w+)"', head) == ["s7", "s8"]
    assert "columns=s7%2Cs8" in picked  # kept in paging and view links
    no_js = client.get("/projects/demo/files", params=[("col", "s2"), ("col", "s3")]).text
    head = section(no_js, "<thead>", "</thead>")
    assert re.findall(r'title="Copy state in (\w+)"', head) == ["s2", "s3"]
    many = ",".join(f"s{i}" for i in range(1, 9))
    capped = client.get("/projects/demo/files", params={"columns": many}).text
    assert len(re.findall(r'title="Copy state in', section(capped, "<thead>", "</thead>"))) == 6
    # the inspector and file page always list every copy
    inspector = client.get("/projects/demo/files/1/inspector").text
    assert inspector.count('class="copy-card"') == 8
    assert client.get("/projects/demo/files/1").text.count('<td class="strong"><a href') == 8


@pytest.mark.req("UI-12")
def test_file_param_renders_the_inspector_beside_the_table() -> None:
    files = series_files()
    files[1] = files[1].model_copy(update={"is_latest": False})
    factory = Factory(files=files)
    html = ui_client(factory).get("/projects/demo/files", params={"file": "2"}).text
    assert 'class="files-layout has-inspector"' in html
    inspector = section(html, '<aside id="inspector"', "</aside>")
    assert "x_v002.csv" in inspector
    assert "1 copy missing" in inspector
    assert 'href="/projects/demo/files/2/download"' in inspector
    assert "Ignore from all rules" in inspector
    assert 'href="/projects/demo/files/2">Open page' in inspector
    assert "hash2" in inspector and "md5" in inspector
    assert "instrument_mode" in inspector and "&lt;script&gt;" in inspector
    timeline = section(inspector, '<ol class="timeline">', "</ol>")
    assert timeline.count("<li") == 3
    assert 'class="current" aria-current="true"' in timeline
    assert 'data-inspect="/projects/demo/files/3/inspector"' in timeline
    assert 'class=" gone"' in timeline
    assert 'href="/projects/demo/files" data-close' in inspector
    assert 'class="row sel"' in rows_by_id(html)["2"]


@pytest.mark.req("UI-12")
@pytest.mark.parametrize("file", ["99", "abc"])
def test_an_unknown_file_param_shows_no_inspector(file: str) -> None:
    html = (
        ui_client(Factory(files=series_files()))
        .get("/projects/demo/files", params={"file": file})
        .text
    )
    assert '<aside id="inspector" class="inspector" aria-label="File inspector"' in html
    assert section(html, '<aside id="inspector"', ">").rstrip().endswith("hidden")


@pytest.mark.req("UI-12")
def test_inspector_fragment_for_javascript() -> None:
    client = ui_client(Factory(files=series_files()))
    resp = client.get(
        "/projects/demo/files/2/inspector", params={"back": "view=latest&file=2&evil=1"}
    )
    assert resp.status_code == 200
    assert "<html" not in resp.text and '<div class="insp" data-file="2">' in resp.text
    assert 'name="return_query" value="view=latest&amp;file=2"' in resp.text
    assert 'href="/projects/demo/files?view=latest" data-close' in resp.text
    other = Factory(files=[make_file(9, project="other")])
    for path in ("/projects/demo/files/9/inspector", "/projects/demo/files/x/inspector"):
        missing = ui_client(other).get(path)
        assert missing.status_code == 404 and "not in this project" in missing.text


@pytest.mark.req("UI-12")
def test_inspector_passes_on_api_errors() -> None:
    resp = ui_client(Factory(error=HelioPuddleError(503, "down"))).get(
        "/projects/demo/files/1/inspector"
    )
    assert resp.status_code == 503


@pytest.mark.req("UI-12", "UI-04")
def test_ignore_toggle_in_the_table_returns_to_the_same_files_view() -> None:
    factory = fleet_factory()
    client = ui_client(factory)
    html = client.get("/projects/demo/files", params={"view": "ignored"}).text
    row = rows_by_id(html)["4"]
    assert 'aria-label="Include in rules: data/file-4.csv"' in row
    assert 'name="return_query" value="view=ignored"' in row
    resp = client.post(
        "/projects/demo/files/4/rules-ignore",
        data={"ignored": "false", "return_to": "files", "return_query": "view=ignored"},
        follow_redirects=False,
    )
    assert resp.headers["location"] == "/projects/demo/files?view=ignored"
    assert factory.calls("set_rules_ignored") == [(4, False)]
    bare = client.post(
        "/projects/demo/files/4/rules-ignore",
        data={"ignored": "true", "return_to": "files"},
        follow_redirects=False,
    )
    assert bare.headers["location"] == "/projects/demo/files"
    assert client.post("/projects/demo/files/4/rules-ignore", data={}).status_code == 400


@pytest.mark.req("UI-12")
def test_download_streams_the_content_through_the_client() -> None:
    factory = Factory(files=[make_file(1)])
    resp = ui_client(factory).get("/projects/demo/files/1/download")
    assert resp.status_code == 200
    assert resp.content == b"a,b\n1,2\n"
    assert resp.headers["content-disposition"] == "attachment; filename*=UTF-8''file-1.csv"
    assert all(c.closed for c in factory.made)


@pytest.mark.req("UI-12")
def test_download_without_an_available_copy_or_in_another_project_is_404() -> None:
    no_copy = Factory(files=[make_file(1, state=M)])
    resp = ui_client(no_copy).get("/projects/demo/files/1/download")
    assert resp.status_code == 404 and "no available copy" in resp.text
    other = Factory(files=[make_file(1, project="other")])
    assert ui_client(other).get("/projects/demo/files/1/download").status_code == 404
    broken = Factory(files=[make_file(1)], error=HelioPuddleError(503, "down"))
    assert ui_client(broken).get("/projects/demo/files/1/download").status_code == 503
    assert all(c.closed for c in no_copy.made + other.made + broken.made)

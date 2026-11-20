"""File list filters in the web UI."""

from __future__ import annotations

import pytest

from heliopuddle.server.schemas import (
    FileState,
)
from heliopuddle.server.ui.pages.files.filters import FileFilters
from tests.unit import _ui_helpers as _fixtures
from tests.unit._ui_helpers import (
    Factory,
    files_find,
    filter_files,
    find_kwargs,
    make_file,
    rows_by_id,
    section,
    ui_client,
)

_no_prefect_env = _fixtures._no_prefect_env


@pytest.mark.req("UI-06")
def test_filters_are_plain_get_forms_without_javascript() -> None:
    html = ui_client(Factory(files=filter_files())).get("/projects/demo/files").text
    chips = section(html, '<div class="chips"', '<div class="chipbar-right">')
    forms = chips.split("<form")[1:]
    assert len(forms) >= 5
    for form in forms:
        assert 'method="get"' in form and 'action="/projects/demo/files"' in form
    assert 'name="path"' in chips
    assert 'name="latest_only" value="1"' in chips
    assert 'name="include_deleted" value="1"' in chips
    for state in FileState:
        assert f'name="state" value="{state.value}"' in chips
    assert 'name="storage_location"' in chips
    assert 'name="content_from"' in chips and 'name="content_to"' in chips
    assert "Clear filters" not in chips


@pytest.mark.req("UI-06")
def test_filters_are_passed_to_find_and_shown_back_as_chips() -> None:
    factory = Factory(files=filter_files())
    params = [
        ("path", " data/x_*.csv "),
        ("latest_only", "on"),
        ("state", "available"),
        ("state", "missing"),
        ("include_deleted", "1"),
    ]
    resp = ui_client(factory).get("/projects/demo/files", params=params)
    assert resp.status_code == 200
    assert files_find(factory) == [
        find_kwargs(
            path="data/x_*.csv",
            latest_only=True,
            state=[FileState.AVAILABLE, FileState.MISSING],
            include_deleted=True,
        )
    ]
    chips = section(resp.text, '<div class="chips"', "<details")
    assert 'Path: <span class="chip-value">data/x_*.csv</span>' in chips
    assert 'State: <span class="chip-value">available</span>' in chips
    assert 'State: <span class="chip-value">missing</span>' in chips
    assert "Latest only" in chips and "Include deleted" in chips
    # each chip removes its own filter and keeps the others
    assert (
        'href="/projects/demo/files?state=available&amp;state=missing&amp;latest_only=1'
        '&amp;include_deleted=1"' in chips
    )
    assert (
        'href="/projects/demo/files?path=data%2Fx_%2A.csv&amp;state=missing&amp;latest_only=1'
        '&amp;include_deleted=1"' in chips
    )
    assert 'class="chip-clear" href="/projects/demo/files"' in resp.text
    assert 'name="state" value="available" checked' in resp.text
    assert 'value="data/x_*.csv"' in resp.text


@pytest.mark.req("UI-06")
def test_filters_narrow_the_list() -> None:
    client = ui_client(Factory(files=filter_files()))

    def ids(params: dict[str, str]) -> set[str]:
        html = client.get("/projects/demo/files", params=params).text
        return set(rows_by_id(html)) if '<table class="ft">' in html else set()

    assert ids({"latest_only": "1"}) == {"3", "4"}
    assert ids({"path": "data/x_*"}) == {"2", "3"}  # the deleted v1 is hidden by default
    assert ids({"include_deleted": "1"}) == {"1", "2", "3", "4"}
    assert ids({"state": "missing"}) == {"4"}
    deleted = client.get("/projects/demo/files", params={"include_deleted": "1"}).text
    assert 'class="row deleted"' in rows_by_id(deleted)["1"]


@pytest.mark.req("UI-06")
def test_list_shows_version_and_latest_badge() -> None:
    html = ui_client(Factory(files=filter_files())).get("/projects/demo/files").text
    assert ">Ver</th>" in html
    rows = rows_by_id(html)
    assert "v3" in rows["3"] and "pill-latest" in rows["3"]
    assert "v2" in rows["2"] and "pill-latest" not in rows["2"]
    assert "unversioned" in rows["4"]


@pytest.mark.req("UI-06")
def test_paging_keeps_filters() -> None:
    files = [make_file(i) for i in range(1, 61)]
    client = ui_client(Factory(files=files))
    params = {"limit": 25, "offset": 25, "path": "data/*", "latest_only": "1"}
    html = client.get("/projects/demo/files", params=params).text
    assert 'href="/projects/demo/files?path=data%2F%2A&amp;latest_only=1&amp;limit=25"' in html
    assert "?path=data%2F%2A&amp;latest_only=1&amp;limit=25&amp;offset=50" in html
    assert 'type="hidden" name="limit" value="25"' in html


@pytest.mark.req("UI-06")
def test_no_matches_says_so() -> None:
    resp = ui_client(Factory(files=filter_files())).get(
        "/projects/demo/files", params={"path": "zzz*"}
    )
    assert resp.status_code == 200
    assert "No files match these filters" in resp.text
    assert '<table class="ft">' not in resp.text


@pytest.mark.req("UI-06")
@pytest.mark.parametrize(
    ("value", "expected"),
    [("1", True), ("on", True), ("TRUE", True), ("yes", True), ("0", False), ("", False)],
)
def test_checkbox_values(value: str, expected: bool) -> None:
    assert FileFilters.parse({"latest_only": [value]}).latest_only is expected
    assert FileFilters.parse({"include_deleted": [value]}).include_deleted is expected


@pytest.mark.req("UI-06")
def test_empty_filters_are_inactive() -> None:
    filters = FileFilters.parse({"path": [""], "state": [""], "latest_only": [""]})
    assert not filters.active
    assert filters.query() == []


@pytest.mark.req("UI-06")
def test_all_filters_round_trip_through_the_query() -> None:
    raw = {
        "view": ["attention"],
        "path": ["a/*"],
        "latest_only": ["on"],
        "state": ["deleting,missing"],
        "storage_location": ["archive"],
        "content_from": ["2026-01-01"],
        "content_to": ["2026-01-31"],
        "include_deleted": ["yes"],
    }
    filters = FileFilters.parse(raw)
    assert filters.active
    query = filters.query()
    assert query == [
        ("view", "attention"),
        ("path", "a/*"),
        ("state", "deleting"),
        ("state", "missing"),
        ("storage_location", "archive"),
        ("content_from", "2026-01-01"),
        ("content_to", "2026-01-31"),
        ("latest_only", "1"),
        ("include_deleted", "1"),
    ]
    again: dict[str, list[str]] = {}
    for key, value in query:
        again.setdefault(key, []).append(value)
    assert FileFilters.parse(again) == filters


@pytest.mark.req("UI-06")
@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"state": "bogus"}, "Unknown state &#39;bogus&#39;"),
        ({"view": "nope"}, "Unknown view &#39;nope&#39;"),
        ({"content_from": "yesterday"}, "Invalid date for content_from"),
    ],
)
def test_unknown_state_view_or_date_is_a_friendly_400(params: dict[str, str], message: str) -> None:
    factory = Factory(files=filter_files())
    resp = ui_client(factory).get("/projects/demo/files", params=params)
    assert resp.status_code == 400
    assert "<html" in resp.text
    assert message in resp.text
    assert factory.made == []


@pytest.mark.req("UI-06")
def test_invalid_paging_value_is_a_friendly_400() -> None:
    resp = ui_client(Factory()).get("/projects/demo/files", params={"limit": "lots"})
    assert resp.status_code == 400
    assert "<html" in resp.text
    assert "Invalid value for limit" in resp.text

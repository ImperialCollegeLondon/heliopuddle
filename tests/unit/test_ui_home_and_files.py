"""Home page, file list, paging and Prefect links of the web UI (UI-01, UI-02, UI-03, UI-05)."""

from __future__ import annotations

import pytest

from heliopuddle.server.schemas import (
    FileState,
)
from heliopuddle.server.ui import (
    human_size,
    prefect_ui_url,
)
from heliopuddle.settings import Settings
from tests.unit import _ui_helpers as _fixtures
from tests.unit._ui_helpers import (
    Factory,
    files_find,
    find_kwargs,
    make_file,
    rows_by_id,
    section,
    ui_client,
)

_no_prefect_env = _fixtures._no_prefect_env


@pytest.mark.req("UI-01")
def test_root_redirects_to_the_overview_of_a_single_project() -> None:
    factory = Factory(projects=["demo"])
    resp = ui_client(factory).get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/projects/demo"
    assert factory.made[0].project is None
    assert factory.made[0].closed


@pytest.mark.req("UI-01")
def test_root_lists_projects_when_several() -> None:
    factory = Factory(projects=["alpha", "beta"])
    resp = ui_client(factory).get("/")
    assert resp.status_code == 200
    assert 'href="/projects/alpha"' in resp.text
    assert 'href="/projects/beta"' in resp.text


@pytest.mark.req("UI-01")
def test_root_with_no_projects_says_so() -> None:
    resp = ui_client(Factory(projects=[])).get("/")
    assert resp.status_code == 200
    assert "No projects are configured" in resp.text


@pytest.mark.req("UI-01")
def test_file_list_shows_path_size_states_created_and_creator() -> None:
    factory = Factory(files=[make_file(1), make_file(2, state=FileState.UNKNOWN)])
    resp = ui_client(factory).get("/projects/demo/files")
    assert resp.status_code == 200
    html = resp.text
    rows = rows_by_id(html)
    assert "file-1.csv" in rows["1"] and "data/" in rows["1"]
    assert "1.5 KiB" in rows["1"] and "3.0 KiB" in rows["2"]
    assert "alice" in rows["1"]
    assert 'class="st st-available"' in rows["1"]
    assert 'class="st st-unknown"' in rows["2"]
    assert 'title="2026-01-02 03:04:05"' in rows["1"]
    head = section(html, "<thead>", "</thead>")
    for header in ("Path", "Size", "primary", "Created", "Created by"):
        assert f">{header}</th>" in head, header
    assert factory.made[0].project == "demo"
    assert files_find(factory) == [find_kwargs()]


@pytest.mark.req("UI-01")
def test_file_without_a_copy_in_a_column_shows_no_copy() -> None:
    html = ui_client(Factory(files=[make_file(1, state=None)])).get("/projects/demo/files").text
    assert 'class="st st-none"' in rows_by_id(html)["1"]
    assert 'aria-label="no copy"' in rows_by_id(html)["1"]


@pytest.mark.req("UI-01")
def test_file_list_empty_state() -> None:
    resp = ui_client(Factory(files=[])).get("/projects/demo/files")
    assert resp.status_code == 200
    assert "No files yet" in resp.text
    assert '<table class="ft">' not in resp.text


@pytest.mark.req("UI-01")
def test_file_list_paging_links() -> None:
    factory = Factory(files=[make_file(i) for i in range(1, 61)])
    client = ui_client(factory)

    first = client.get("/projects/demo/files", params={"limit": 25})
    assert "1\u201325 of 60" in first.text
    assert 'href="/projects/demo/files?limit=25&amp;offset=25"' in first.text
    assert 'rel="prev"' not in first.text

    middle = client.get("/projects/demo/files", params={"limit": 25, "offset": 25})
    assert "26\u201350 of 60" in middle.text
    assert 'rel="prev" href="/projects/demo/files?limit=25"' in middle.text
    assert 'rel="next" href="/projects/demo/files?limit=25&amp;offset=50"' in middle.text

    last = client.get("/projects/demo/files", params={"limit": 25, "offset": 50})
    assert "51\u201360 of 60" in last.text
    assert 'rel="next"' not in last.text
    assert files_find(factory)[-1] == find_kwargs(limit=25, offset=50)


@pytest.mark.req("UI-01", "UI-12")
@pytest.mark.parametrize(
    ("asked", "used"), [(1, 25), (25, 25), (26, 50), (100, 100), (150, 200), (100000, 200)]
)
def test_page_sizes_snap_to_25_50_100_or_200(asked: int, used: int) -> None:
    factory = Factory(files=[])
    ui_client(factory).get("/projects/demo/files", params={"limit": asked, "offset": -5})
    assert files_find(factory) == [find_kwargs(limit=used, offset=0)]


@pytest.mark.req("UI-01", "UI-12")
def test_rows_per_page_menu_offers_the_four_sizes() -> None:
    html = ui_client(Factory(files=[make_file(1)])).get("/projects/demo/files").text
    menu = section(html, '<details class="menu rows-menu">', "</details>")
    for size in (25, 100, 200):
        assert f'href="/projects/demo/files?limit={size}">{size} rows</a>' in menu
    assert 'href="/projects/demo/files" aria-current="true">50 rows</a>' in menu


@pytest.mark.req("UI-01")
def test_user_header_is_forwarded_to_the_client() -> None:
    factory = Factory()
    client = ui_client(factory, user_header="X-Remote-User")
    client.get("/projects/demo/files", headers={"X-Remote-User": "bob"})
    first = len(factory.made)
    client.get("/projects/demo/files")
    assert factory.made[0].user == "bob"
    assert factory.made[first].user is None


@pytest.mark.req("UI-01")
@pytest.mark.parametrize("path", ["/", "/projects/demo", "/projects/demo/files", "/search"])
def test_every_page_links_to_the_prefect_ui_from_runs(path: str) -> None:
    factory = Factory(projects=["demo", "other"])
    resp = ui_client(factory, prefect_ui_url="http://prefect.example:4200").get(path)
    assert resp.status_code == 200
    assert '<a class="hdr-link" href="http://prefect.example:4200"' in resp.text
    assert "Runs" in resp.text


@pytest.mark.req("UI-01")
def test_prefect_link_derived_from_prefect_api_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PREFECT_API_URL", "http://127.0.0.1:4200/api")
    resp = ui_client(Factory()).get("/projects/demo")
    assert 'href="http://127.0.0.1:4200"' in resp.text


@pytest.mark.req("UI-01")
def test_prefect_link_omitted_when_unknown() -> None:
    resp = ui_client(Factory()).get("/projects/demo")
    assert "Runs <span" not in resp.text
    assert "Prefect" not in resp.text


@pytest.mark.req("UI-01")
@pytest.mark.parametrize(
    ("configured", "env", "expected"),
    [
        ("http://p:4200/", "http://ignored/api", "http://p:4200"),
        (None, "http://p:4200/api", "http://p:4200"),
        (None, "http://p:4200/api/", "http://p:4200"),
        (None, "https://p.example/prefect/api", "https://p.example/prefect"),
        (None, "http://p:4200", "http://p:4200"),
        (None, "", None),
        (None, None, None),
    ],
)
def test_prefect_ui_url_derivation(
    monkeypatch: pytest.MonkeyPatch, configured: str | None, env: str | None, expected: str | None
) -> None:
    if env is not None:
        monkeypatch.setenv("PREFECT_API_URL", env)
    assert prefect_ui_url(Settings(prefect_ui_url=configured)) == expected


@pytest.mark.req("UI-01")
@pytest.mark.parametrize(
    ("size", "text"),
    [
        (0, "0 B"),
        (1023, "1023 B"),
        (1024, "1.0 KiB"),
        (5 * 1024**2, "5.0 MiB"),
        (3 * 1024**3 + 1, "3.0 GiB"),
        (2 * 1024**5, "2.0 PiB"),
        (5 * 1024**6, "5120.0 PiB"),
    ],
)
def test_human_size(size: int, text: str) -> None:
    assert human_size(size) == text

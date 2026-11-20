"""The web UI file page and version history."""

from __future__ import annotations

from typing import Any

import pytest

from heliopuddle.client import HelioPuddleError
from heliopuddle.server.ui.shared.fileinfo import version_label
from tests.unit import _ui_helpers as _fixtures
from tests.unit._ui_helpers import (
    Factory,
    make_file,
    make_version,
    rows_by_id,
    section,
    series_files,
    ui_client,
)

_no_prefect_env = _fixtures._no_prefect_env


@pytest.mark.req("UI-02")
def test_file_page_shows_file_properties() -> None:
    factory = Factory(files=series_files())
    resp = ui_client(factory).get("/projects/demo/files/2")
    assert resp.status_code == 200
    html = resp.text
    for text in (
        "data/x_v002.csv",
        "x_v002.csv",
        "2.0 KiB",
        "(2050 bytes)",
        "hash2",
        "(md5)",
        "v2",
        "2025-12-31 00:00:00",
        "2026-01-02 03:04:05</span> by alice",
        "2026-02-03 04:05:06</span> by bob",
        "data/x_v{v}.csv",
    ):
        assert text in html, text
    assert "<dt>Deleted</dt>" not in html
    calls = [c for c in factory.made[0].calls if c[0] in ("get_file", "list_versions")]
    assert calls == [("get_file", 2), ("list_versions", (7, True))]
    assert factory.made[0].project == "demo"
    assert factory.made[0].closed


@pytest.mark.req("UI-02")
def test_file_page_metadata_is_escaped() -> None:
    html = ui_client(Factory(files=series_files())).get("/projects/demo/files/2").text
    assert "<script>x</script>" not in html
    assert "&lt;script&gt;x&lt;/script&gt;" in html
    assert "&lt;b&gt;key&lt;/b&gt;" in html
    assert "instrument_mode" in html and "survey" in html
    assert "<pre>3</pre>" in html
    assert "&#34;a&#34;: [" in html


@pytest.mark.req("UI-02")
def test_file_page_lists_copies_with_their_states() -> None:
    html = ui_client(Factory(files=series_files())).get("/projects/demo/files/2").text
    copies = section(html, 'class="tbl copies"', "</table>")
    assert "primary" in copies and "archive" in copies
    assert 'class="pill st-available"' in copies and "available" in copies
    assert 'class="pill st-missing"' in copies
    assert 'title="2050 bytes"' in copies
    assert "2026-01-02 03:04:05" in copies  # mtime
    assert 'title="2026-02-03 04:05:06"' in copies  # last seen


@pytest.mark.req("UI-02")
def test_file_page_version_history() -> None:
    html = ui_client(Factory(files=series_files())).get("/projects/demo/files/2").text
    history = section(html, 'class="tbl versions"', "</table>")
    rows = history.split("<tr")[2:]  # skip the header row
    assert len(rows) == 3
    # Newest first, each linking to its page.
    assert 'href="/projects/demo/files/3"' in rows[0]
    assert 'href="/projects/demo/files/2"' in rows[1]
    assert 'href="/projects/demo/files/1"' in rows[2]
    assert "pill-latest" in rows[0]
    assert "pill-latest" not in rows[1] + rows[2]
    assert 'class="current"' in rows[1] and 'aria-current="true"' in rows[1]
    assert "this file" in rows[1]
    assert "current" not in rows[0] + rows[2]
    assert 'class=" deleted"' in rows[2]
    assert "deleted 2026-02-03 04:05:06" in rows[2]


@pytest.mark.req("UI-02")
def test_file_page_of_a_deleted_latest_text_version_file() -> None:
    files = [make_version(5, text="001.0002", is_latest=True, deleted=True)]
    resp = ui_client(Factory(files=files)).get("/projects/demo/files/5")
    html = resp.text
    assert resp.status_code == 200
    assert '<dt>Version</dt><dd class="mono">001.0002</dd>' in html
    assert "<dt>Deleted</dt>" in html
    assert 'class="current deleted"' in html
    assert "No metadata." in html
    assert "No copies." in html
    assert "Download" not in html  # a deleted file has no content to download


@pytest.mark.req("UI-02")
def test_file_page_version_history_of_a_text_series() -> None:
    files = [
        make_version(1, text="001.0002"),
        make_version(2, text="001.0010", is_latest=True),
        make_version(3, text="<b>x</b>", deleted=True),
    ]
    html = ui_client(Factory(files=files)).get("/projects/demo/files/2").text
    history = section(html, 'class="tbl versions"', "</table>")
    rows = history.split("<tr")[2:]
    assert ["001.0010" in rows[1], "001.0002" in rows[2]] == [True, True]
    assert "&lt;b&gt;x&lt;/b&gt;" in rows[0] and "<b>x</b>" not in html
    assert '<dt>Version</dt><dd class="mono">001.0010</dd>' in html


@pytest.mark.req("UI-02")
def test_file_page_unversioned_file() -> None:
    resp = ui_client(Factory(files=[make_file(4)])).get("/projects/demo/files/4")
    assert resp.status_code == 200
    assert "unversioned" in resp.text


@pytest.mark.req("UI-02")
@pytest.mark.parametrize("file_id", ["99", "abc", "-1"])
def test_file_page_unknown_file_is_404(file_id: str) -> None:
    resp = ui_client(Factory(files=series_files())).get(f"/projects/demo/files/{file_id}")
    assert resp.status_code == 404
    assert "<html" in resp.text
    assert "Traceback" not in resp.text


@pytest.mark.req("UI-02")
def test_file_page_of_a_file_in_another_project_is_404() -> None:
    factory = Factory(files=[make_version(9, version=1, project="other")])
    resp = ui_client(factory).get("/projects/demo/files/9")
    assert resp.status_code == 404
    assert "not found in project" in resp.text
    assert factory.made[0].calls == [("get_file", 9)]


@pytest.mark.req("UI-02")
def test_file_page_api_error_other_than_404_is_shown() -> None:
    factory = Factory(error=HelioPuddleError(503, "db down"))
    resp = ui_client(factory).get("/projects/demo/files/2")
    assert resp.status_code == 503


@pytest.mark.req("UI-02")
def test_file_list_paths_link_to_the_file_page() -> None:
    html = ui_client(Factory(files=[make_file(1)])).get("/projects/demo/files").text
    row = rows_by_id(html)["1"]
    assert 'href="/projects/demo/files/1"' in row
    assert 'data-inspect="/projects/demo/files/1/inspector"' in row


@pytest.mark.req("UI-02")
def test_file_page_links_include_root_path() -> None:
    client = ui_client(Factory(files=series_files()), ui_root_path="/hp")
    page = client.get("/hp/projects/demo/files/2")
    assert 'href="/hp/projects/demo/files/3"' in page.text
    assert 'href="/hp/projects/demo"' in page.text
    listing = client.get("/hp/projects/demo/files")
    assert 'href="/hp/projects/demo/files/3"' in listing.text


@pytest.mark.req("UI-02")
@pytest.mark.parametrize(
    ("kwargs", "label"),
    [
        ({"version": 4}, "v4"),
        ({"version": 0}, "v0"),
        ({"text": "001.0004"}, "001.0004"),
        ({"text": "rc-2"}, "rc-2"),
        ({}, "unversioned"),
    ],
)
def test_version_label(kwargs: dict[str, Any], label: str) -> None:
    assert version_label(make_version(1, **kwargs)) == label

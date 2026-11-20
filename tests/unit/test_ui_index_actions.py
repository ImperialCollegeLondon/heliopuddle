"""Storage location page, index actions and cross-site POST protection in the web UI."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from heliopuddle.client import HelioPuddleError
from tests.unit import _ui_helpers as _fixtures
from tests.unit._ui_helpers import (
    RUN_ID,
    SAME_ORIGIN,
    Factory,
    m3_storage_locations,
    notice_of,
    section,
    series_files,
    ui_client,
)

_no_prefect_env = _fixtures._no_prefect_env


@pytest.mark.req("UI-07", "UI-13")
def test_storage_location_page_shows_url_external_changes_and_index_times() -> None:
    client = ui_client(Factory(storage_locations=m3_storage_locations()))
    primary = client.get("/projects/demo/storage/primary").text
    assert "file:///srv/primary" in primary
    config = section(primary, 'id="cfg-heading"')
    assert "External changes</dt><dd>accepted" in config
    assert "2026-04-05 06:07:08" in config  # last full index
    assert 'Last incremental</dt><dd><span class="muted">never</span>' in config
    archive = client.get("/projects/demo/storage/archive").text
    assert "Restricted" in section(archive, 'id="cfg-heading"')
    assert "not accepted · recorded as unknown" in archive
    assert 'Last full index</dt><dd><span class="muted">never</span>' in archive


@pytest.mark.req("UI-07")
@pytest.mark.parametrize("mode", ["full", "incremental"])
def test_index_buttons_post_a_run_and_redirect_to_a_notice_linking_to_prefect(mode: str) -> None:
    factory = Factory(storage_locations=m3_storage_locations())
    client = ui_client(factory, prefect_ui_url="https://prefect.example/ui/")
    page = client.get("/projects/demo/storage/primary").text
    assert 'action="/projects/demo/storage/primary/index"' in page
    assert f'name="mode" value="{mode}"' in page

    resp = client.post(
        "/projects/demo/storage/primary/index",
        data={"mode": mode},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert urlsplit(location).path == "/projects/demo/storage/primary"
    assert parse_qs(urlsplit(location).query) == {"index_run": [RUN_ID], "index_mode": [mode]}
    assert factory.calls("trigger_index") == [("primary", mode)]

    notice = notice_of(client.get(location).text)
    assert f"{mode.capitalize()} index run submitted" in notice
    assert f'href="https://prefect.example/ui/runs/flow-run/{RUN_ID}"' in notice


@pytest.mark.req("UI-07")
def test_index_now_defaults_to_full_without_a_mode() -> None:
    factory = Factory()
    resp = ui_client(factory).post(
        "/projects/demo/storage/primary/index", data={}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert factory.calls("trigger_index") == [("primary", "full")]


@pytest.mark.req("UI-07")
def test_index_notice_without_prefect_url_shows_the_run_id() -> None:
    html = (
        ui_client(Factory())
        .get("/projects/demo/storage/primary", params={"index_run": RUN_ID, "index_mode": "full"})
        .text
    )
    notice = notice_of(html)
    assert RUN_ID in notice and "<a " not in notice


@pytest.mark.req("UI-07")
def test_index_notice_ignores_bad_values() -> None:
    client = ui_client(Factory())
    html = client.get("/projects/demo/storage/primary", params={"index_run": "not-a-uuid"}).text
    assert 'role="status"' not in html
    odd = client.get(
        "/projects/demo/storage/primary", params={"index_run": RUN_ID, "index_mode": "weird"}
    ).text
    assert "Index run submitted" in notice_of(odd)


@pytest.mark.req("UI-07")
def test_index_now_unknown_mode_is_a_friendly_400() -> None:
    factory = Factory()
    resp = ui_client(factory).post(
        "/projects/demo/storage/primary/index", data={"mode": "sideways"}
    )
    assert resp.status_code == 400
    assert "Unknown index mode" in resp.text
    assert factory.calls("trigger_index") == []


@pytest.mark.req("UI-07")
def test_index_now_api_error_renders_error_page() -> None:
    factory = Factory(action_error=HelioPuddleError(409, "storage location 'old' is inactive"))
    resp = ui_client(factory).post("/projects/demo/storage/old/index", data={"mode": "full"})
    assert resp.status_code == 409
    assert "is inactive" in resp.text


@pytest.mark.req("UI-07")
def test_index_now_is_post_only() -> None:
    resp = ui_client(Factory()).get("/projects/demo/storage/primary/index")
    assert resp.status_code in (404, 405)


@pytest.mark.req("UI-07")
@pytest.mark.parametrize(
    "headers",
    [
        {"Sec-Fetch-Site": "cross-site"},
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
        {"Referer": "https://evil.example/page"},
    ],
)
def test_cross_site_posts_are_rejected(headers: dict[str, str]) -> None:
    factory = Factory(files=series_files())
    client = ui_client(factory)
    for path, data in (
        ("/projects/demo/storage/primary/index", {"mode": "full"}),
        ("/projects/demo/files/2/accept", {"storage_location": "archive"}),
        ("/projects/demo/files/2/rules-ignore", {"ignored": "true"}),
    ):
        resp = client.post(path, data=data, headers=headers)
        assert resp.status_code == 403, path
        assert "Cross-site form posts are not allowed" in resp.text
    assert factory.made == []


@pytest.mark.req("UI-07")
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Sec-Fetch-Site": "same-origin"},
        {"Origin": "http://testserver"},
        {"Referer": "http://testserver/projects/demo"},
        {"Origin": "https://hp.example", "X-Forwarded-Host": "hp.example"},
    ],
)
def test_same_origin_posts_are_allowed(headers: dict[str, str]) -> None:
    resp = ui_client(Factory()).post(
        "/projects/demo/storage/primary/index",
        data={"mode": "full"},
        headers=headers,
        follow_redirects=False,
    )
    assert resp.status_code == 303

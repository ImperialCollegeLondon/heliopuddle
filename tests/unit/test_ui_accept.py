"""Accepting unknown copies as is through the web UI."""

from __future__ import annotations

import pytest

from heliopuddle.client import HelioPuddleError
from heliopuddle.server.schemas import (
    FileState,
)
from tests.unit import _ui_helpers as _fixtures
from tests.unit._ui_helpers import (
    LATER,
    SAME_ORIGIN,
    Factory,
    file_with,
    make_file,
    make_version,
    rows_by_id,
    ui_client,
    unknown_file,
)

_no_prefect_env = _fixtures._no_prefect_env


@pytest.mark.req("UI-07")
def test_file_page_and_inspector_have_accept_for_each_unknown_copy() -> None:
    client = ui_client(Factory(files=[unknown_file()]))
    for path in ("/projects/demo/files/2", "/projects/demo/files/2/inspector"):
        html = client.get(path).text
        assert html.count("Accept as is") == 2, path
        assert 'name="storage_location" value="archive"' in html
        assert 'name="storage_location" value="old"' in html
        assert 'name="storage_location" value="primary"' not in html


@pytest.mark.req("UI-07")
def test_deleted_file_has_no_accept_button() -> None:
    deleted = unknown_file().model_copy(update={"deleted_at": LATER})
    client = ui_client(Factory(files=[deleted]))
    assert "Accept as is" not in client.get("/projects/demo/files/2").text
    assert "Accept as is" not in client.get("/projects/demo/files/2/inspector").text


@pytest.mark.req("UI-07")
def test_accept_as_is_calls_the_client_and_redirects_to_the_file_page() -> None:
    factory = Factory(files=[unknown_file()])
    resp = ui_client(factory).post(
        "/projects/demo/files/2/accept",
        data={"storage_location": "archive"},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/projects/demo/files/2"
    assert factory.calls("accept_file") == [(2, "archive")]


@pytest.mark.req("UI-07", "UI-12")
def test_accept_from_the_inspector_returns_to_the_files_page_with_the_inspector_open() -> None:
    factory = Factory(files=[unknown_file()])
    html = (
        ui_client(factory)
        .get("/projects/demo/files", params={"view": "attention", "file": "2"})
        .text
    )
    assert 'name="return_query" value="view=attention&amp;file=2"' in html
    resp = ui_client(factory).post(
        "/projects/demo/files/2/accept",
        data={
            "storage_location": "archive",
            "return_to": "files",
            "return_query": "view=attention&file=2&next=//evil.example",
        },
        follow_redirects=False,
    )
    assert resp.headers["location"] == "/projects/demo/files?view=attention&file=2"


@pytest.mark.req("UI-07")
def test_accept_as_is_without_storage_location_uses_the_default() -> None:
    factory = Factory(files=[unknown_file()])
    resp = ui_client(factory).post("/projects/demo/files/2/accept", follow_redirects=False)
    assert resp.status_code == 303
    assert factory.calls("accept_file") == [(2, None)]


@pytest.mark.req("UI-07")
def test_accept_as_is_file_of_another_project_is_404() -> None:
    factory = Factory(files=[make_version(9, version=1, project="other")])
    resp = ui_client(factory).post("/projects/demo/files/9/accept", data={})
    assert resp.status_code == 404
    assert factory.calls("accept_file") == []


@pytest.mark.req("UI-07")
def test_accept_as_is_conflict_renders_error_page() -> None:
    factory = Factory(
        files=[unknown_file()], action_error=HelioPuddleError(409, "copy is not unknown")
    )
    resp = ui_client(factory).post(
        "/projects/demo/files/2/accept", data={"storage_location": "archive"}
    )
    assert resp.status_code == 409
    assert "copy is not unknown" in resp.text


@pytest.mark.req("UI-07")
def test_files_without_pending_or_available_copies_are_not_latest_eligible() -> None:
    files = [
        file_with(1, ("primary", FileState.AVAILABLE)),
        file_with(2, ("primary", FileState.PENDING)),
        file_with(3, ("primary", FileState.MISSING), is_latest=False),
        file_with(4, ("primary", FileState.UNKNOWN), is_latest=False),
        make_file(5, state=FileState.DELETED, is_latest=False, deleted_at=LATER),
    ]
    client = ui_client(Factory(files=files))
    rows = rows_by_id(client.get("/projects/demo/files", params={"include_deleted": "1"}).text)
    label = "not latest-eligible"
    assert label not in rows["1"] and label not in rows["2"]
    assert label in rows["3"] and label in rows["4"]
    assert label not in rows["5"]  # deleted files are labelled deleted instead
    assert label in client.get("/projects/demo/files/3").text
    assert label in client.get("/projects/demo/files/3/inspector").text
    assert label not in client.get("/projects/demo/files/1").text

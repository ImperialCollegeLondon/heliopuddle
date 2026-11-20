"""Error pages of the web UI."""

from __future__ import annotations

import httpx
import pytest

from heliopuddle.client import HelioPuddleError, NotFoundError
from tests.unit import _ui_helpers as _fixtures
from tests.unit._ui_helpers import Factory, ui_client

_no_prefect_env = _fixtures._no_prefect_env


@pytest.mark.req("UI-01")
@pytest.mark.parametrize(
    "path", ["/projects/nope", "/projects/nope/files", "/projects/nope/storage"]
)
def test_unknown_project_renders_404_page(path: str) -> None:
    factory = Factory(error=NotFoundError(404, "project 'nope' not found"))
    resp = ui_client(factory).get(path)
    assert resp.status_code == 404
    assert "project &#39;nope&#39; not found" in resp.text
    assert "Traceback" not in resp.text
    assert factory.made[0].closed


@pytest.mark.req("UI-01")
def test_api_error_status_is_passed_through() -> None:
    resp = ui_client(Factory(error=HelioPuddleError(503, "db down"))).get("/projects/demo")
    assert resp.status_code == 503
    assert "db down" in resp.text


@pytest.mark.req("UI-01")
def test_api_error_detail_that_is_not_text_is_shown() -> None:
    error = HelioPuddleError(409, {"detail": "busy"})
    resp = ui_client(Factory(error=error)).get("/projects/demo")
    assert resp.status_code == 409
    assert "busy" in resp.text


@pytest.mark.req("UI-01")
@pytest.mark.parametrize("path", ["/", "/projects/demo", "/projects/demo/files"])
def test_api_unreachable_renders_502_page(path: str) -> None:
    error = httpx.ConnectError("connection refused")
    resp = ui_client(Factory(error=error)).get(path)
    assert resp.status_code == 502
    assert "API is unreachable" in resp.text
    assert "Traceback" not in resp.text


@pytest.mark.req("UI-01")
def test_unexpected_error_renders_500_page() -> None:
    resp = ui_client(Factory(error=RuntimeError("boom"))).get("/projects/demo")
    assert resp.status_code == 500
    assert "<html" in resp.text
    assert "boom" not in resp.text
    assert "Traceback" not in resp.text


@pytest.mark.req("UI-01")
def test_unknown_route_renders_404_page() -> None:
    resp = ui_client(Factory()).get("/no/such/page")
    assert resp.status_code == 404
    assert "<html" in resp.text
    assert "Back to projects" in resp.text

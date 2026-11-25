"""Security headers on API and UI responses (AUTH-19)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from heliopuddle.server.api import create_api
from heliopuddle.server.context import AppContext
from heliopuddle.server.security_headers import SecurityHeadersMiddleware
from tests.unit._ui_helpers import ui_client
from tests.unit.test_ui_preview import factory_with, image_file

EXPECTED = {
    "x-frame-options": "DENY",
    "x-content-type-options": "nosniff",
    "referrer-policy": "same-origin",
}


def _check(headers: Any) -> None:
    for name, value in EXPECTED.items():
        assert headers[name] == value
    assert "frame-ancestors 'none'" in headers["content-security-policy"]


@pytest.fixture
def api(ctx: AppContext) -> Iterator[TestClient]:
    with TestClient(create_api(ctx), headers={"X-Forwarded-User": "alice"}) as c:
        yield c


@pytest.mark.req("AUTH-19")
def test_ui_pages_and_errors_carry_the_headers() -> None:
    client = ui_client(factory_with(files=[image_file()]))
    _check(client.get("/").headers)
    _check(client.get("/no/such/page").headers)
    _check(client.get("/static/nothing.css").headers)


@pytest.mark.req("AUTH-19")
def test_api_responses_carry_the_headers(api: TestClient) -> None:
    _check(api.get("/health").headers)
    _check(api.get("/projects/nope/files").headers)


@pytest.mark.req("AUTH-19")
def test_a_routes_own_csp_is_kept_and_gets_frame_ancestors() -> None:
    raw = ui_client(factory_with(files=[image_file(size=4)], content=b"\x89PNG")).get(
        "/projects/demo/files/1/raw"
    )
    csp = raw.headers["content-security-policy"]
    assert csp.startswith("sandbox; default-src 'none'")
    assert csp.endswith("frame-ancestors 'none'")
    assert csp.count("frame-ancestors") == 1


@pytest.mark.req("AUTH-19")
def test_middleware_never_overrides_headers_a_route_set() -> None:
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/own")
    def own() -> PlainTextResponse:
        return PlainTextResponse(
            "x",
            headers={
                "X-Frame-Options": "SAMEORIGIN",
                "Content-Security-Policy": "frame-ancestors 'self'",
            },
        )

    headers = TestClient(app).get("/own").headers
    assert headers["x-frame-options"] == "SAMEORIGIN"
    assert headers["content-security-policy"] == "frame-ancestors 'self'"


# --------------------------------------------------- configurable frame ancestors


@pytest.mark.req("AUTH-19")
@pytest.mark.parametrize(
    ("ancestors", "frame_options"),
    [("'none'", "DENY"), ("'self'", "SAMEORIGIN"), ("'self' https://portal.test", None)],
)
def test_frame_ancestors_setting_sets_csp_and_x_frame_options(
    ancestors: str, frame_options: str | None
) -> None:
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware, frame_ancestors=ancestors)

    @app.get("/page")
    def page() -> PlainTextResponse:
        return PlainTextResponse("x")

    headers = TestClient(app).get("/page").headers
    assert headers["content-security-policy"] == f"frame-ancestors {ancestors}"
    # X-Frame-Options cannot name other origins; browsers then use the CSP alone
    assert headers.get("x-frame-options") == frame_options
    assert headers["x-content-type-options"] == "nosniff"


@pytest.mark.req("AUTH-19")
def test_ui_pages_follow_the_frame_ancestors_setting() -> None:
    client = ui_client(factory_with(files=[image_file()]), frame_ancestors="'self'")
    headers = client.get("/").headers
    assert headers["content-security-policy"] == "frame-ancestors 'self'"
    assert headers["x-frame-options"] == "SAMEORIGIN"
    raw = ui_client(
        factory_with(files=[image_file(size=4)], content=b"\x89PNG"), frame_ancestors="'self'"
    ).get("/projects/demo/files/1/raw")
    assert raw.headers["content-security-policy"].endswith("frame-ancestors 'self'")


@pytest.mark.req("AUTH-19")
def test_api_responses_follow_the_frame_ancestors_setting(ctx: AppContext) -> None:
    ctx.settings = ctx.settings.model_copy(update={"frame_ancestors": "'self'"})
    with TestClient(create_api(ctx), headers={"X-Forwarded-User": "alice"}) as c:
        headers = c.get("/health").headers
    assert headers["content-security-policy"] == "frame-ancestors 'self'"
    assert headers["x-frame-options"] == "SAMEORIGIN"


@pytest.mark.req("AUTH-19")
@pytest.mark.parametrize(
    "value", ["", "   ", "'self'; script-src *", "'self', https://a.test", "'self'\nX-Evil: 1"]
)
def test_frame_ancestors_setting_rejects_other_directives(value: str) -> None:
    from pydantic import ValidationError

    from heliopuddle.settings import Settings

    with pytest.raises(ValidationError, match="frame_ancestors"):
        Settings(frame_ancestors=value)

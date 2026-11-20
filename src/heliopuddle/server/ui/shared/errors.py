"""Errors the UI raises and the exception handlers that turn them into error pages."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from heliopuddle.client import HelioPuddleError
from heliopuddle.server.ui.auth import session as auth_session

if TYPE_CHECKING:
    from heliopuddle.server.ui.shared.context import UiContext


class BadRequestError(Exception):
    """Invalid UI input; rendered as a friendly 400 page."""


class CrossSiteError(Exception):
    """A form post from another site; rendered as a friendly 403 page."""


def install_error_handlers(app: FastAPI, ui: UiContext) -> None:
    """Render every error as the UI's error page.

    Args:
        app: the UI app.
        ui: the shared UI context (for the error page and the login redirect).
    """

    @app.exception_handler(CrossSiteError)
    def _cross_site(request: Request, exc: CrossSiteError) -> Response:
        return ui.error_page(
            request, 403, "Error 403", "Cross-site form posts are not allowed. Reload the page."
        )

    @app.exception_handler(BadRequestError)
    def _bad_request(request: Request, exc: BadRequestError) -> Response:
        return ui.error_page(request, 400, "Error 400", str(exc))

    @app.exception_handler(ValidationError)
    def _invalid_spec(request: Request, exc: ValidationError) -> Response:
        messages = [
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        ]
        return ui.error_page(request, 400, "Invalid rule specification", "; ".join(messages))

    @app.exception_handler(RequestValidationError)
    def _invalid_params(request: Request, exc: RequestValidationError) -> Response:
        names = sorted({str(e["loc"][-1]) for e in exc.errors() if e.get("loc")})
        return ui.error_page(request, 400, "Error 400", f"Invalid value for {', '.join(names)}.")

    @app.exception_handler(HelioPuddleError)
    def _api_error(request: Request, exc: HelioPuddleError) -> Response:
        if ui.login_mode and exc.status_code == 401:  # the token was refused: log in again
            request.session.clear()
            return auth_session.login_redirect(
                request, auth_session.current_target(request, ui.root_path)
            )
        status = exc.status_code if 400 <= exc.status_code < 600 else 502
        detail = exc.detail if isinstance(exc.detail, str) else repr(exc.detail)
        return ui.error_page(request, status, f"Error {status}", detail)

    @app.exception_handler(httpx.HTTPError)
    def _api_unreachable(request: Request, exc: httpx.HTTPError) -> Response:
        return ui.error_page(
            request,
            502,
            "Error 502",
            "The API is unreachable. Check that the heliopuddle API is running.",
        )

    @app.exception_handler(StarletteHTTPException)
    def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
        return ui.error_page(request, exc.status_code, f"Error {exc.status_code}", str(exc.detail))

    @app.exception_handler(Exception)
    def _unexpected(request: Request, exc: Exception) -> Response:
        return ui.error_page(request, 500, "Error 500", "Something went wrong.")

    if ui.login_mode:

        @app.exception_handler(auth_session.LoginRequired)
        def _login_required(request: Request, exc: auth_session.LoginRequired) -> Response:
            return auth_session.login_redirect(request, exc.next_path)

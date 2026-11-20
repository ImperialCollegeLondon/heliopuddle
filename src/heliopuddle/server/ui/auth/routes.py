"""The login pages: ``/login``, ``/logout`` and the OIDC ``/auth/callback`` (AUTH-16).

* ``local`` mode: ``/login`` is a form; the password goes to the API's token
  endpoint through the client.
* ``oidc`` mode: ``/login`` starts an authorization-code flow with ``state``
  and PKCE; ``/auth/callback`` finishes it.

The router is only added to the app in those two modes (in ``header`` mode none
of this is active). See :mod:`heliopuddle.server.ui.auth.session` for the session.
"""

from __future__ import annotations

import hmac
import logging
import secrets
import time
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from heliopuddle.client import HelioPuddleError
from heliopuddle.server.auth.errors import AuthError
from heliopuddle.server.auth.oidc import OidcProvider, OidcStartupError
from heliopuddle.server.auth.throttle import get_throttle
from heliopuddle.server.ui.auth.oidc import external_url, pkce_challenge
from heliopuddle.server.ui.auth.session import SessionUser, read_user, safe_next, store_user
from heliopuddle.server.ui.shared.context import Ui, UiContext
from heliopuddle.server.ui.shared.forms import FormData, SameOrigin

logger = logging.getLogger(__name__)

DEFAULT_EXPIRES_IN = 300.0
"""Token lifetime assumed when the provider omits ``expires_in``."""

_BAD_LOGIN = (400, 401, 403)
"""Statuses of a refused token request: wrong credentials, shown generically."""

router = APIRouter()


def _go(ui: UiContext, next_path: str) -> Response:
    return RedirectResponse(ui.root_path + next_path, status_code=303)


def _form_page(
    ui: UiContext, request: Request, next_path: str, *, status_code: int = 200, **context: Any
) -> Response:
    response = ui.render(
        request,
        "login.html",
        status_code,
        next=next_path,
        mode=ui.settings.auth_mode,
        login_page=True,  # no Projects or Runs links before signing in
        **context,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def _bad_credentials(ui: UiContext, request: Request, next_path: str, username: str) -> Response:
    return _form_page(
        ui,
        request,
        next_path,
        status_code=401,
        message="Incorrect username or password.",
        username=username,
    )


def _too_many(
    ui: UiContext, request: Request, next_path: str, username: str, wait: int
) -> Response:
    minutes = max(1, -(-wait // 60))
    response = _form_page(
        ui,
        request,
        next_path,
        status_code=429,
        message=f"Too many failed logins. Try again in about {minutes} minute"
        f"{'' if minutes == 1 else 's'}.",
        username=username,
    )
    response.headers["Retry-After"] = str(wait)
    return response


@router.get("/login", name="ui_login")
def login(
    request: Request, ui: Ui, next: str | None = None, logged_out: str | None = None
) -> Response:
    """Show the login form, or start the OIDC flow, or skip ahead if already signed in."""
    next_path = safe_next(next, ui.root_path)
    if read_user(request) is not None:
        return _go(ui, next_path)
    if ui.settings.auth_mode == "oidc" and not logged_out:
        return _oidc_begin(ui, request, next_path)
    notice = "You have been logged out." if logged_out else None
    return _form_page(ui, request, next_path, notice=notice, username="")


@router.post("/login", name="ui_login_submit", dependencies=[SameOrigin])
def login_submit(request: Request, ui: Ui, form: dict[str, str] = FormData) -> Response:
    """Check a local user's password through the API and start their session."""
    next_path = safe_next(form.get("next"), ui.root_path)
    if ui.settings.auth_mode != "local":
        return _go(ui, next_path)
    username, password = form.get("username", ""), form.get("password", "")
    throttle = get_throttle(ui.settings)
    ip = request.client.host if request.client else None
    wait = throttle.retry_after(username, ip)
    if wait is not None:
        return _too_many(ui, request, next_path, username, wait)
    if not username or not password:
        return _bad_credentials(ui, request, next_path, username)
    try:
        with ui.factory(None, username=username, password=password) as client:
            result = client.login()
            me = client.me()
    except HelioPuddleError as e:
        if e.status_code == 429:  # the API's throttle (AUTH-18)
            return _too_many(ui, request, next_path, username, 60)
        if e.status_code in _BAD_LOGIN:
            # The API counts the username; count the real client address here, because
            # the API only sees this server's.
            throttle.record_failure(None, ip)
            return _bad_credentials(ui, request, next_path, username)
        logger.warning("UI login failed: the API answered %s", e.status_code or "no reply")
        return ui.error_page(
            request, 502, "Login unavailable", "The API could not be reached to log you in."
        )
    expires = result.expires_at.timestamp() if result.expires_at else time.time() + 60
    store_user(
        request,
        SessionUser(me.username or username, tuple(me.roles), result.access_token, expires),
    )
    return _go(ui, next_path)


@router.post("/logout", name="ui_logout", dependencies=[SameOrigin])
def logout(request: Request, ui: Ui) -> Response:
    """End the session (and the OIDC provider's, when it has an end-session URL)."""
    request.session.clear()
    logged_out = f"{request.url_for('ui_login').path}?logged_out=1"
    if ui.oidc is not None:
        try:
            end_session = ui.oidc.get().end_session_url
        except OidcStartupError:
            end_session = None  # the provider is down: still log out of hp
        if end_session:
            # RP-initiated logout: end the provider's SSO session too, so the next
            # page does not sign the user straight back in. No id_token_hint: the
            # id token is not kept (the cookie is size-limited).
            query = urlencode(
                {
                    "client_id": ui.settings.oidc_client_id or "",
                    "post_logout_redirect_uri": external_url(request, "ui_login") + "?logged_out=1",
                }
            )
            separator = "&" if "?" in end_session else "?"
            return RedirectResponse(f"{end_session}{separator}{query}", status_code=303)
    return RedirectResponse(logged_out, status_code=303)


def _oidc_provider(ui: UiContext) -> OidcProvider:
    assert ui.oidc is not None
    return ui.oidc.get()


def _oidc_begin(ui: UiContext, request: Request, next_path: str) -> Response:
    try:
        idp = _oidc_provider(ui)
    except OidcStartupError as e:
        logger.warning("OIDC login: identity provider unavailable: %s", e)
        return ui.error_page(
            request,
            503,
            "Login unavailable",
            "The identity provider is unavailable. Try again later.",
        )
    if idp.authorization_url is None:
        return ui.error_page(
            request,
            503,
            "Login unavailable",
            "The identity provider has no authorization endpoint.",
        )
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    redirect_uri = external_url(request, "ui_oidc_callback")
    request.session.clear()
    request.session["oidc"] = {
        "state": state,
        "verifier": verifier,
        "redirect_uri": redirect_uri,
        "next": next_path,
    }
    query = urlencode(
        {
            "response_type": "code",
            "client_id": idp.client_id,
            "redirect_uri": redirect_uri,
            "scope": "openid",
            "state": state,
            "code_challenge": pkce_challenge(verifier),
            "code_challenge_method": "S256",
        }
    )
    separator = "&" if "?" in idp.authorization_url else "?"
    return RedirectResponse(f"{idp.authorization_url}{separator}{query}", status_code=303)


def _callback_failed(ui: UiContext, request: Request, status: int, message: str) -> Response:
    request.session.clear()
    return ui.error_page(request, status, "Login failed", message)


@router.get("/auth/callback", name="ui_oidc_callback")
def oidc_callback(
    request: Request,
    ui: Ui,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> Response:
    """Finish the authorization-code flow: check ``state``, swap the code for a token."""
    if ui.settings.auth_mode != "oidc" or ui.oidc is None:
        return _go(ui, "/")
    flow = request.session.pop("oidc", None)
    if not isinstance(flow, dict):
        return _callback_failed(
            ui, request, 400, "The login session expired or was never started. Log in again."
        )
    expected = str(flow.get("state", ""))
    if not state or not expected or not hmac.compare_digest(state, expected):
        return _callback_failed(ui, request, 400, "The login response did not match. Log in again.")
    if error or not code:
        return _callback_failed(ui, request, 401, "The identity provider refused the login.")
    try:
        idp = _oidc_provider(ui)
        reply = idp.token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": str(flow.get("redirect_uri", "")),
                "code_verifier": str(flow.get("verifier", "")),
            }
        )
    except OidcStartupError as e:
        logger.warning("OIDC callback: identity provider unavailable: %s", e)
        return _callback_failed(ui, request, 503, "The identity provider is unavailable.")
    except AuthError as e:
        status = 401 if e.status_code == 401 else 503
        return _callback_failed(ui, request, status, "The identity provider refused the login.")
    token = reply.get("access_token")
    if not isinstance(token, str) or not token:
        return _callback_failed(ui, request, 401, "The identity provider sent no access token.")
    try:
        expires_in = float(reply.get("expires_in", DEFAULT_EXPIRES_IN))
    except (TypeError, ValueError):
        expires_in = DEFAULT_EXPIRES_IN
    try:
        with ui.factory(None, token=token) as client:
            me = client.me()
    except HelioPuddleError as e:
        logger.warning("OIDC login: the API refused the new token (%s)", e.status_code)
        return _callback_failed(ui, request, 401, "The API did not accept the login.")
    store_user(
        request,
        SessionUser(me.username or "", tuple(me.roles), token, time.time() + expires_in),
    )
    return _go(ui, safe_next(str(flow.get("next", "/")), ui.root_path))

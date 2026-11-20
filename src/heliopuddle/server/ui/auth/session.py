"""UI sessions and the login guard (AUTH-16).

The UI never validates a token: it holds one for the user, in a signed cookie,
and passes it to the API through :class:`~heliopuddle.client.HelioPuddleClient`.
See docs/design/auth.md ("UI").

The session cookie is ``hp_session``: ``HttpOnly``, ``SameSite=Lax`` (the CSRF
defence of the UI's forms), ``Secure`` when the request is https, and scoped to
the UI root path. The login pages themselves are in
:mod:`heliopuddle.server.ui.auth.routes`.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from starlette.datastructures import MutableHeaders
from starlette.middleware.sessions import SessionMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from heliopuddle.server.auth.secret import get_secret
from heliopuddle.server.schemas import Me
from heliopuddle.settings import Settings

SESSION_COOKIE = "hp_session"
"""Name of the session cookie."""

_PUBLIC_ROUTES = frozenset({"ui_login", "ui_login_submit", "ui_oidc_callback", "ui_logout"})


class LoginRequired(Exception):
    """The request needs a login; handled by redirecting to ``/login?next=...``."""

    def __init__(self, next_path: str) -> None:
        super().__init__(next_path)
        self.next_path = next_path


@dataclass(frozen=True)
class SessionUser:
    """The signed-in user held by a session.

    Attributes:
        username: the user name shown in the nav.
        roles: the roles from ``GET /auth/me`` at login.
        token: the bearer token (never rendered or logged).
        expires_at: when the token expires, as a Unix timestamp.
    """

    username: str
    roles: tuple[str, ...]
    token: str
    expires_at: float

    def can(self, role: str, project: str | None = None) -> bool:
        """Whether the roles grant ``role`` on ``project`` (the server's own rule)."""
        return Me(username=self.username, roles=list(self.roles), mode="local").can(role, project)


def safe_next(value: str | None, root_path: str = "") -> str:
    """Return a post-login redirect target that cannot leave the UI.

    Only a local path is accepted: it must start with a single ``/`` (not
    ``//``), contain no backslash, control character, scheme or host, and not
    point at the login pages (which would loop). A leading UI root path is
    removed, because redirects add it back.

    Args:
        value: the untrusted ``next`` value.
        root_path: the UI root path, without a trailing slash.

    Returns:
        A path starting with ``/`` that is relative to the UI root, or ``/``.
    """
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    if "\\" in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        return "/"
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return "/"
    if root_path and (value == root_path or value.startswith(root_path + "/")):
        value = value[len(root_path) :] or "/"
        if value.startswith("//"):
            return "/"
    if parts.path in ("/login", "/auth/callback", "/logout"):
        return "/"
    return value


def session_key(settings: Settings) -> str:
    """The session-signing key, derived from the auth secret for this purpose only.

    Raises:
        heliopuddle.server.auth.secret.SecretKeyError: there is no auth secret.
    """
    secret = get_secret(settings)
    return hmac.new(secret, b"heliopuddle-ui-session", hashlib.sha256).hexdigest()


def forwarded(request_headers: Any, name: str) -> str:
    return str(request_headers.get(name, "")).split(",")[0].strip().lower()


class _SecureCookie:
    """Add ``Secure`` to the session cookie when the request came over https.

    Starlette's session middleware has one fixed ``https_only`` flag. https is
    seen from the connection scheme or from a proxy's ``X-Forwarded-Proto``.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        https = scope.get("scheme") == "https" or any(
            k == b"x-forwarded-proto" and v.split(b",")[0].strip().lower() == b"https"
            for k, v in scope.get("headers", [])
        )

        async def send_wrapper(message: Message) -> None:
            if https and message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                cookies = headers.getlist("set-cookie")
                if cookies:
                    del headers["set-cookie"]
                    for cookie in cookies:
                        if cookie.startswith(SESSION_COOKIE + "="):
                            cookie += "; secure"
                        headers.append("set-cookie", cookie)
            await send(message)

        await self.app(scope, receive, send_wrapper)


def install_session(app: FastAPI, settings: Settings, root_path: str) -> None:
    """Add the signed session cookie to ``app`` (not used in ``header`` mode).

    Raises:
        heliopuddle.server.auth.secret.SecretKeyError: there is no auth secret
            (for example a UI-only process that cannot read or create one).
    """
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_key(settings),
        session_cookie=SESSION_COOKIE,
        max_age=None,
        path=root_path or "/",
        same_site="lax",
        https_only=False,
    )
    app.add_middleware(_SecureCookie)


def read_user(request: Request) -> SessionUser | None:
    """The session's user, or None if there is none or its token has expired."""
    if "session" not in request.scope:  # a 500 page is rendered outside the session middleware
        return None
    data = request.session
    token, name, roles, expires = (
        data.get("token"),
        data.get("username"),
        data.get("roles"),
        data.get("expires_at"),
    )
    if not (
        isinstance(token, str)
        and isinstance(name, str)
        and isinstance(roles, list)
        and isinstance(expires, int | float)
    ):
        return None
    if expires <= time.time():
        return None
    return SessionUser(name, tuple(str(r) for r in roles), token, float(expires))


def store_user(request: Request, user: SessionUser) -> None:
    request.session.clear()  # a new login never reuses an old session
    request.session.update(
        token=user.token, username=user.username, roles=list(user.roles), expires_at=user.expires_at
    )


def login_redirect(request: Request, next_path: str) -> Response:
    """Redirect to ``/login``, returning to ``next_path`` (relative to the UI root) afterwards."""
    base = request.url_for("ui_login").path
    target = f"{base}?{urlencode({'next': next_path})}" if next_path != "/" else base
    return RedirectResponse(target, status_code=303)


def current_target(request: Request, root_path: str) -> str:
    """The request's own path and query as a safe ``next`` value (``/`` for a non-GET)."""
    if request.method not in ("GET", "HEAD"):
        return "/"
    path = request.url.path
    if root_path and path.startswith(root_path):
        path = path[len(root_path) :]
    query = request.url.query
    return safe_next(f"{path}?{query}" if query else path, root_path)


def make_guard(root_path: str) -> Callable[[Request], None]:
    """A dependency for every UI route: require a live session, or raise :class:`LoginRequired`."""

    def guard(request: Request) -> None:
        route = request.scope.get("route")
        if getattr(route, "name", None) in _PUBLIC_ROUTES:
            return
        if read_user(request) is not None:
            return
        raise LoginRequired(current_target(request, root_path))

    return guard

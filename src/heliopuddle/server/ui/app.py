"""The web UI app: projects home, project pages and file search, read through the client.

The UI never touches the database (UI-05). Every page builds a
:class:`~heliopuddle.client.HelioPuddleClient` for the request and renders the
result with Jinja2. In ``header`` auth mode the client forwards the caller's user
header; in ``local`` and ``oidc`` mode the user logs in (see
:mod:`heliopuddle.server.ui.auth`) and the client carries the session's token.

Pages follow docs/design/ui.md: the projects home, and per project an Overview,
Files (with the inspector), a file page, Storage and Rules, plus search. Each
page is a package under :mod:`heliopuddle.server.ui.pages` with its own router;
this module only builds the app and includes them. Code several pages use is in
:mod:`heliopuddle.server.ui.shared`.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from heliopuddle.server.auth.oidc import OidcProvider
from heliopuddle.server.security_headers import SecurityHeadersMiddleware
from heliopuddle.server.ui import auth as ui_auth
from heliopuddle.server.ui.auth import routes as auth_routes
from heliopuddle.server.ui.pages import files, home, overview, rules, search, storage
from heliopuddle.server.ui.shared.clients import (
    ClientFactory,
    default_api_url,
    default_client_factory,
)
from heliopuddle.server.ui.shared.context import UiContext
from heliopuddle.server.ui.shared.errors import install_error_handlers
from heliopuddle.server.ui.shared.format import human_size
from heliopuddle.server.ui.shared.templating import make_templates
from heliopuddle.settings import Settings, prefect_ui_url

__all__ = ["ClientFactory", "create_ui", "default_api_url", "human_size", "prefect_ui_url"]

_HERE = Path(__file__).resolve().parent


class _RootPathMiddleware:
    """Set the ASGI ``root_path`` (the prefix a reverse proxy serves the UI under)."""

    def __init__(self, app: ASGIApp, root_path: str) -> None:
        self.app = app
        self.root_path = root_path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            scope = {**scope, "root_path": self.root_path}
        await self.app(scope, receive, send)


def create_ui(
    settings: Settings,
    client_factory: ClientFactory | None = None,
    *,
    oidc_provider: Callable[[], OidcProvider] | None = None,
) -> FastAPI:
    """Build the UI app.

    Routes are relative to the UI root (``/``, ``/projects/{project}``,
    ``/projects/{project}/files``, ``/search``, ``/static/...``; see
    docs/design/ui.md); the runtime mounts the app or serves it on its own port.
    Links and redirects are root-relative paths that include the ASGI
    ``root_path``, so they work behind a reverse proxy and when mounted.

    Args:
        settings: process settings. ``ui_root_path`` is set as the ASGI
            ``root_path`` of every request (it replaces any root path set by
            the server or a parent mount); ``ui_api_url``, ``port`` and ``api_root_path`` give
            the API URL (see :func:`default_api_url`); ``user_header`` names the
            header forwarded to the API in ``header`` mode; ``auth_mode`` selects
            login (``local``, ``oidc``) or none (``header``); ``prefect_ui_url`` is
            the Prefect link.
        client_factory: builds a client, see :class:`ClientFactory`. Defaults to a
            :class:`HelioPuddleClient` for :func:`default_api_url`.
        oidc_provider: builds the OIDC provider on the first login (``oidc`` mode).
            Defaults to ``OidcProvider.discover(settings)``; tests inject a fake issuer.

    Returns:
        A FastAPI app serving HTML pages.

    Raises:
        heliopuddle.server.auth.secret.SecretKeyError: the auth mode is not
            ``header`` and there is no signing key for the session cookie (AUTH-06).
    """
    root_path = settings.ui_root_path.rstrip("/")
    login_mode = settings.auth_mode != "header"
    guard = ui_auth.make_guard(root_path)
    app = FastAPI(
        title="heliopuddle UI",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[Depends(guard)] if login_mode else None,
    )
    if login_mode:
        ui_auth.install_session(app, settings, root_path)  # fails fast without a key
    if root_path:
        app.add_middleware(_RootPathMiddleware, root_path=root_path)
    app.add_middleware(SecurityHeadersMiddleware)
    app.state.root_path = root_path
    app.mount("/static", StaticFiles(directory=_HERE / "static"), name="ui_static")

    templates = make_templates()
    files.install(templates.env)
    ui = UiContext(
        settings=settings,
        factory=client_factory or default_client_factory(settings),
        templates=templates,
        root_path=root_path,
        login_mode=login_mode,
        oidc=ui_auth.OidcProviderCache(oidc_provider or (lambda: OidcProvider.discover(settings)))
        if settings.auth_mode == "oidc"
        else None,
    )
    app.state.ui = ui

    if login_mode:
        app.include_router(auth_routes.router)
    for page in (home, overview, files, storage, rules, search):
        app.include_router(page.router)
    install_error_handlers(app, ui)
    return app

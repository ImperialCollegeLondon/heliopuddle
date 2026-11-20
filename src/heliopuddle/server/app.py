"""Assemble the ASGI apps that ``server start`` serves (DEP-02, UI-03).

The API is mounted at ``/api/v1`` and the UI at ``/``. Root paths for reverse
proxies are applied by :class:`RootPathMiddleware`, which accepts requests
both with the prefix (proxy keeps it, or a direct call to the prefixed URL)
and without it (proxy strips it).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from fastapi import FastAPI
from starlette.types import ASGIApp, Receive, Scope, Send

import heliopuddle
from heliopuddle.server.context import AppContext
from heliopuddle.server.ui import ClientFactory, create_ui
from heliopuddle.settings import Settings

API_PREFIX = "/api/v1"


def normalize_root_path(root_path: str) -> str:
    """Return ``root_path`` as ``/prefix`` (no trailing slash), or ``""``."""
    root_path = root_path.strip().rstrip("/")
    if root_path and not root_path.startswith("/"):
        root_path = "/" + root_path
    return root_path


class RootPathMiddleware:
    """Serve an app under a path prefix (``root_path``) for reverse proxies.

    Sets the ASGI ``root_path`` so generated URLs carry the prefix, and
    accepts request paths with or without it.

    Args:
        app: the app to wrap.
        root_path: the prefix, such as ``/hp``.
    """

    def __init__(self, app: ASGIApp, root_path: str) -> None:
        self.app = app
        self.root_path = normalize_root_path(root_path)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in {"http", "websocket"} and self.root_path:
            scope = dict(scope)
            path: str = scope["path"]
            rp = self.root_path
            if path != rp and not path.startswith(rp + "/"):
                scope["path"] = rp + path
                if "raw_path" in scope and scope["raw_path"] is not None:
                    scope["raw_path"] = rp.encode() + scope["raw_path"]
            scope["root_path"] = rp
        await self.app(scope, receive, send)


def _lifespan_of(apps: list[FastAPI]) -> Callable[[FastAPI], Any]:
    """A lifespan that runs the mounted apps' lifespans (Starlette does not)."""

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            for app in apps:
                await stack.enter_async_context(app.router.lifespan_context(app))
            yield

    return lifespan


def create_app(
    settings: Settings,
    ctx: AppContext | None,
    *,
    api: bool = True,
    ui: bool = True,
    client_factory: ClientFactory | None = None,
) -> FastAPI:
    """Build the app for one uvicorn server.

    Args:
        settings: process settings.
        ctx: the app context for the API (required when ``api``).
        api: mount the API at ``/api/v1``.
        ui: mount the UI at ``/``.
        client_factory: the UI's client factory (default: HTTP to the API, see
            :func:`heliopuddle.server.ui.default_api_url`).

    Returns:
        A FastAPI app. Root paths are not applied here; see
        :func:`wrap_root_path`.

    Raises:
        ValueError: if neither part is enabled, or ``api`` without a context.
    """
    from heliopuddle.server.api import create_api

    if not (api or ui):
        raise ValueError("create_app needs the API, the UI or both")
    mounted: list[tuple[str, FastAPI]] = []
    if api:
        if ctx is None:
            raise ValueError("the API needs an AppContext")
        mounted.append((API_PREFIX, create_api(ctx)))
    if ui:
        mounted.append(("/", create_ui(settings, client_factory)))
    app = FastAPI(
        title="heliopuddle",
        version=heliopuddle.__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=_lifespan_of([sub for _, sub in mounted]),
    )
    for path, sub in mounted:  # the API first: "/" would match everything
        app.mount(path, sub)
    return app


def wrap_root_path(app: ASGIApp, root_path: str) -> ASGIApp:
    """Wrap ``app`` in :class:`RootPathMiddleware` when ``root_path`` is set."""
    root_path = normalize_root_path(root_path)
    return RootPathMiddleware(app, root_path) if root_path else app

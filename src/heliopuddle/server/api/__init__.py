"""The HTTP API, mounted at ``/api/v1``.

Use :func:`create_api` to build the app; routes are relative to the mount
point (``/health``, ``/projects``, ...).
"""

from __future__ import annotations

from fastapi import FastAPI

import heliopuddle
from heliopuddle.server.api import (
    auth,
    files,
    health,
    projects,
    rules,
    runs,
    search,
    uploads,
    users,
)
from heliopuddle.server.api.deps import install_auth_handlers
from heliopuddle.server.api.errors import install_validation_scrub
from heliopuddle.server.context import AppContext, set_context
from heliopuddle.server.security_headers import SecurityHeadersMiddleware


def create_api(ctx: AppContext | None = None) -> FastAPI:
    """Build the API app.

    Args:
        ctx: the context to serve. If given, it is also installed as the
            process context (:func:`set_context`) so that tasks called inline,
            such as ``verify_file``, use the same config and database. If None,
            each request uses :func:`get_context`, built lazily from settings.

    Returns:
        A FastAPI app with routes relative to its mount point.
    """
    app = FastAPI(title="heliopuddle", version=heliopuddle.__version__)
    if ctx is not None:
        set_context(ctx)
    app.state.ctx = ctx
    app.add_middleware(SecurityHeadersMiddleware)
    install_auth_handlers(app)
    install_validation_scrub(app)
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(projects.router)
    app.include_router(files.router)
    app.include_router(uploads.router)
    app.include_router(runs.router)
    app.include_router(rules.router)
    app.include_router(users.router)
    app.include_router(search.router)
    return app


__all__ = ["create_api"]

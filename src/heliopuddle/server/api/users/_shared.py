"""Helpers shared by the ``/users`` endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from heliopuddle.server.api.deps import Ctx, requires
from heliopuddle.server.auth.roles import ROLE_ADMIN

LOCAL_ONLY = "users are managed by the identity provider unless HELIOPUDDLE_AUTH_MODE is local"


def require_local(ctx: Ctx) -> None:
    """404 outside ``local`` mode.

    This runs after authentication and role authorization (the ``requires``
    dependency), so an unauthenticated caller gets 401 and a non-admin 403 in
    every mode; only a verified ``hp-admin`` learns that there is no user API.
    """
    if ctx.settings.auth_mode != "local":
        raise HTTPException(status_code=404, detail=LOCAL_ONLY)


def new_router() -> APIRouter:
    """A router for one ``/users`` endpoint: global ``hp-admin``, local mode only.

    The dependencies are declared on the endpoint's own router (not the package
    router) so that every route carries its role requirement.
    """
    return APIRouter(
        dependencies=[Depends(requires(ROLE_ADMIN)), Depends(require_local)],
        responses={
            401: {"description": "Not authenticated"},
            403: {"description": "Needs the global hp-admin role"},
            404: {"description": "Unknown user, or not in local auth mode"},
        },
    )


def not_found(username: str) -> HTTPException:
    """A ``404`` for an unknown user."""
    return HTTPException(status_code=404, detail=f"user {username!r} not found")

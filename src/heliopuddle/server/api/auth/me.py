"""``GET /auth/me``: the caller's username, roles and the auth mode."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from heliopuddle.server.api.deps import requires
from heliopuddle.server.auth.principal import Principal
from heliopuddle.server.schemas.auth import Me

router = APIRouter()


@router.get("/auth/me", response_model=Me)
def me(principal: Annotated[Principal, Depends(requires(None))]) -> Me:
    """The caller's username, roles and the auth mode.

    Any valid token will do, but in ``oidc`` mode with ``HELIOPUDDLE_OIDC_INTROSPECT=always``
    the provider is asked whether it is still active, like on every other route.
    """
    return Me(username=principal.username, roles=list(principal.roles), mode=principal.mode)

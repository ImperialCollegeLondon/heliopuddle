"""``GET /auth/config``: how clients authenticate (public)."""

from __future__ import annotations

from fastapi import APIRouter, Request

from heliopuddle.server.api.deps import Ctx
from heliopuddle.server.schemas.auth import AuthConfig

router = APIRouter()


@router.get("/auth/config", response_model=AuthConfig)
def auth_config(request: Request, ctx: Ctx) -> AuthConfig:
    """How clients authenticate (public): the mode and the token and authorization URLs."""
    mode = ctx.settings.auth_mode
    token_url: str | None = None
    authorization_url: str | None = None
    if mode == "local":
        token_url = str(request.url_for("issue_token"))
    elif mode == "oidc":
        token_url = getattr(ctx.oidc, "token_url", None)
        authorization_url = getattr(ctx.oidc, "authorization_url", None)
    return AuthConfig(mode=mode, token_url=token_url, authorization_url=authorization_url)

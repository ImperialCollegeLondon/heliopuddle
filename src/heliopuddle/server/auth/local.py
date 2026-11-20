"""Local mode: verify hp-issued tokens (AUTH-05)."""

from __future__ import annotations

from heliopuddle.server.auth.errors import AuthError
from heliopuddle.server.auth.principal import Principal
from heliopuddle.server.auth.roles import valid_roles
from heliopuddle.server.auth.secret import SecretKeyError, get_secret
from heliopuddle.server.auth.tokens import verify_token
from heliopuddle.server.context import AppContext
from heliopuddle.server.models import users as users_db


def authenticate(token: str, ctx: AppContext) -> Principal:
    """Verify a local access token and return its principal.

    The token is verified (signature, ``alg`` pinned to HS256, ``iss``, ``exp``),
    then the user is re-read by the id in the token: it must exist, be active
    and still have the credentials version the token was issued for, so a
    password change or reset, or a deleted and recreated user, invalidates
    every earlier token at once (no clock resolution involved). The
    principal's roles are the DB roles, not the token's.

    Raises:
        AuthError: the token is invalid or expired, was issued for other
            credentials, or its user is missing or disabled.
    """
    try:
        secret = get_secret(ctx.settings)
    except SecretKeyError:
        raise AuthError("the auth signing key is unavailable", 502) from None
    claims = verify_token(secret, token)
    with ctx.sessionmaker() as session:
        user = users_db.get_user_by_id(session, claims.user_id)
        if user is None or not user.is_active:
            raise AuthError("invalid token")
        if users_db.credentials_version(user) != claims.version:
            raise AuthError("token issued before the credentials changed")
        return Principal(
            username=user.username,
            roles=tuple(valid_roles(user.roles)),
            mode="local",
            token=token,
        )

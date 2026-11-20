"""Local access tokens: HS256 JWTs signed with the auth secret (AUTH-05)."""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass

import jwt

from heliopuddle.server.auth.errors import AuthError

ISSUER = "heliopuddle"
ALGORITHM = "HS256"


@dataclass(frozen=True)
class TokenClaims:
    """The verified claims of a local token.

    Attributes:
        username: the ``sub`` claim.
        roles: the ``realm_access.roles`` claim (informational: the server uses
            the roles in the DB).
        user_id: the ``uid`` claim: the id of the user it was issued to.
        version: the ``cv`` claim: the user's credentials version when it was issued.
        issued_at: the ``iat`` claim, whole seconds since the epoch.
        expires_at: the ``exp`` claim, whole seconds since the epoch.
        token_id: the ``jti`` claim.
    """

    username: str
    roles: tuple[str, ...]
    user_id: int
    version: int
    issued_at: int
    expires_at: int
    token_id: str


def create_token(
    secret: bytes,
    username: str,
    roles: Iterable[str],
    ttl_seconds: int,
    *,
    user_id: int,
    version: int,
    now: float | None = None,
) -> str:
    """Issue a signed access token.

    Args:
        secret: the signing key (:func:`heliopuddle.server.auth.secret.load_secret`).
        username: the ``sub`` claim.
        roles: the user's roles, placed in ``realm_access.roles`` (as Keycloak does).
        ttl_seconds: lifetime in seconds.
        user_id: the user's id (``uid``); a recreated user has a new id.
        version: the user's credentials version (``cv``,
            :func:`heliopuddle.server.models.users.credentials_version`) read from the
            same row the password was verified against.
        now: the issue time (seconds since the epoch); defaults to the clock.

    Returns:
        The compact JWT.
    """
    iat = int(time.time() if now is None else now)
    claims = {
        "iss": ISSUER,
        "sub": username,
        "realm_access": {"roles": list(roles)},
        "uid": user_id,
        "cv": version,
        "iat": iat,
        "exp": iat + ttl_seconds,
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(claims, secret, algorithm=ALGORITHM)


def _int_claim(value: object) -> int:
    """``value`` as an int claim (a JSON number, not a bool); AuthError otherwise."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise AuthError("invalid token")
    return value


def verify_token(secret: bytes, token: str) -> TokenClaims:
    """Verify signature, algorithm (pinned to HS256), issuer and expiry.

    Raises:
        AuthError: the token is malformed, tampered, expired, unsigned
            (``alg=none``), signed with another algorithm, or from another issuer.
    """
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=[ALGORITHM],
            issuer=ISSUER,
            options={"require": ["exp", "iat", "iss", "sub", "jti", "uid", "cv"]},
        )
    except jwt.ExpiredSignatureError:
        raise AuthError("token expired") from None
    except jwt.PyJWTError:
        raise AuthError("invalid token") from None
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        raise AuthError("invalid token")
    iat = _int_claim(claims.get("iat"))
    exp = _int_claim(claims.get("exp"))
    uid = _int_claim(claims.get("uid"))
    cv = _int_claim(claims.get("cv"))
    realm = claims.get("realm_access")
    raw = realm.get("roles") if isinstance(realm, dict) else None
    roles = tuple(r for r in raw if isinstance(r, str)) if isinstance(raw, list) else ()
    return TokenClaims(
        username=sub,
        roles=roles,
        user_id=uid,
        version=cv,
        issued_at=iat,
        expires_at=exp,
        token_id=str(claims.get("jti")),
    )

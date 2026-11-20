"""``POST /auth/token``: exchange a username and password for a token (AUTH-04)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import JSONResponse

from heliopuddle.server.api.auth._shared import NO_STORE, oauth_error, require_local
from heliopuddle.server.api.deps import Ctx
from heliopuddle.server.auth.passwords import verify_dummy, verify_password
from heliopuddle.server.auth.secret import get_secret
from heliopuddle.server.auth.throttle import get_throttle
from heliopuddle.server.auth.tokens import create_token
from heliopuddle.server.models import users as users_db
from heliopuddle.server.schemas.auth import TokenResponse

router = APIRouter()


@router.post(
    "/auth/token",
    response_model=TokenResponse,
    responses={
        400: {"description": "OAuth error: invalid_request, invalid_grant, unsupported_grant_type"},
        404: {"description": "Not in local auth mode"},
        429: {"description": "Too many failed logins; see Retry-After"},
    },
)
def issue_token(
    request: Request,
    ctx: Ctx,
    grant_type: Annotated[str | None, Form()] = None,
    username: Annotated[str | None, Form()] = None,
    password: Annotated[str | None, Form()] = None,
) -> Response:
    """Exchange a username and password for an access token (OAuth2 password grant).

    Unknown users and wrong passwords return the same ``400 invalid_grant`` and
    cost the same time (a dummy hash is verified for an unknown user). A
    disabled user is also ``invalid_grant``. After too many failures for a username
    or from a client address within the window the answer is ``429`` with
    ``Retry-After`` (AUTH-18). Local auth mode only.
    """
    require_local(ctx)
    if grant_type is None:
        return oauth_error("invalid_request", "grant_type is required")
    if grant_type != "password":
        return oauth_error("unsupported_grant_type", "only grant_type=password is supported")
    if not username or password is None:
        return oauth_error("invalid_request", "username and password are required")
    throttle = get_throttle(ctx.settings)
    ip = request.client.host if request.client else None
    wait = throttle.retry_after(username, ip)
    if wait is not None:
        return JSONResponse(
            status_code=429,
            content={
                "error": "too_many_requests",
                "error_description": f"too many failed logins; try again in {wait} seconds",
            },
            headers=NO_STORE | {"Retry-After": str(wait)},
        )
    with ctx.sessionmaker() as session:
        user = users_db.get_user_by_username(session, username)
        if user is None:
            snapshot = None
            password_ok = False
        else:
            snapshot = (user.id, users_db.credentials_version(user), user.is_active)
            password_ok = verify_password(password, user.password_hash)
    if snapshot is None:
        verify_dummy(password)
    if snapshot is None or not password_ok or not snapshot[2]:
        throttle.record_failure(username, ip)
        return oauth_error("invalid_grant", "incorrect username or password")
    user_id, version, _ = snapshot
    # The password was checked against the row read above. Issue the token only if that
    # very row (same id, same credentials version) is still current under a row lock, and
    # bind the token to that version: a reset or a recreated user in between makes this
    # login fail, and a reset right after invalidates the token at once (AUTH-05).
    with ctx.write_session() as session:
        row = users_db.get_user_by_id(session, user_id, for_update=True)
        if row is None or not row.is_active or users_db.credentials_version(row) != version:
            throttle.record_failure(username, ip)
            return oauth_error("invalid_grant", "incorrect username or password")
        users_db.touch_last_login(session, row)
        roles = list(row.roles)
        name = row.username
    throttle.record_success(username)
    ttl = int(ctx.settings.auth_token_ttl.total_seconds())
    token = create_token(
        get_secret(ctx.settings), name, roles, ttl, user_id=user_id, version=version
    )
    body = TokenResponse(access_token=token, expires_in=ttl)
    return JSONResponse(content=body.model_dump(), headers=NO_STORE)

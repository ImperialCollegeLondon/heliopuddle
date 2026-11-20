"""``POST /auth/me/password``: change the caller's own password (AUTH-11)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response

from heliopuddle.server.api.auth._shared import require_local
from heliopuddle.server.api.deps import Ctx, CurrentPrincipal
from heliopuddle.server.auth.passwords import hash_password, verify_password
from heliopuddle.server.models import users as users_db
from heliopuddle.server.schemas.auth import PasswordChange

router = APIRouter()


@router.post(
    "/auth/me/password",
    status_code=204,
    responses={
        400: {"description": "The current password is wrong"},
        404: {"description": "Not in local auth mode"},
    },
)
def change_password(body: PasswordChange, ctx: Ctx, principal: CurrentPrincipal) -> Response:
    """Change the caller's own password (local mode).

    The change sets ``credentials_changed_at``, so every token issued before it,
    including the caller's current one, stops working: log in again. The user row
    is locked and re-read before the current password is checked and stays
    locked through the update, so an admin reset that commits meanwhile can
    neither be missed (the old password is then wrong) nor overwritten.
    """
    require_local(ctx)
    with ctx.write_session() as session:
        user = (
            users_db.get_user_by_username(session, principal.username, for_update=True)
            if principal.username
            else None
        )
        if user is None or not verify_password(body.current_password, user.password_hash):
            raise HTTPException(status_code=400, detail="the current password is incorrect")
        users_db.set_password_hash(session, user, hash_password(body.new_password))
    return Response(status_code=204)

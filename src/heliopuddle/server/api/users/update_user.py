"""``PATCH /users/{username}``: change a password, roles or ``is_active``."""

from __future__ import annotations

from fastapi import HTTPException

from heliopuddle.server.api.deps import Ctx
from heliopuddle.server.api.users._shared import new_router, not_found
from heliopuddle.server.auth.passwords import hash_password
from heliopuddle.server.models import users as users_db
from heliopuddle.server.schemas.auth import UserRead, UserUpdate

router = new_router()


@router.patch(
    "/users/{username}",
    response_model=UserRead,
    responses={409: {"description": "Would remove the last active hp-admin"}},
)
def update_user(username: str, body: UserUpdate, ctx: Ctx) -> UserRead:
    """Change a user's password, roles or ``is_active``; omitted fields stay as they are.

    A new password sets ``credentials_changed_at``, so the user's existing tokens stop
    working. Roles and ``is_active`` apply from the next request. Demoting or disabling the
    last active ``hp-admin`` is ``409``.
    """
    password_hash = None if body.password is None else hash_password(body.password)
    try:
        with ctx.write_session() as session:
            user = users_db.update_user(
                session,
                username,
                password_hash=password_hash,
                roles=body.roles,
                is_active=body.is_active,
            )
            return UserRead.model_validate(user)
    except users_db.UserNotFoundError:
        raise not_found(username) from None
    except users_db.LastAdminError as error:
        raise HTTPException(status_code=409, detail=str(error)) from None

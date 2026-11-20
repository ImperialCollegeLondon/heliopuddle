"""``POST /users``: create a user."""

from __future__ import annotations

from fastapi import HTTPException

from heliopuddle.server.api.deps import Ctx
from heliopuddle.server.api.users._shared import new_router
from heliopuddle.server.auth.passwords import hash_password
from heliopuddle.server.models import users as users_db
from heliopuddle.server.schemas.auth import UserCreate, UserRead

router = new_router()


@router.post(
    "/users",
    status_code=201,
    response_model=UserRead,
    responses={409: {"description": "The username is taken (ignoring case)"}},
)
def create_user(body: UserCreate, ctx: Ctx) -> UserRead:
    """Create a user. The password needs 12 characters and every role must be valid."""
    password_hash = hash_password(body.password)
    try:
        with ctx.write_session() as session:
            user = users_db.create_user(
                session, body.username, password_hash, body.roles, is_active=body.is_active
            )
            return UserRead.model_validate(user)
    except users_db.UserExistsError as error:
        raise HTTPException(status_code=409, detail=str(error)) from None

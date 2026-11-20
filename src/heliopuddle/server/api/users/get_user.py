"""``GET /users/{username}``: one user."""

from __future__ import annotations

from heliopuddle.server.api.deps import Ctx
from heliopuddle.server.api.users._shared import new_router, not_found
from heliopuddle.server.models import users as users_db
from heliopuddle.server.schemas.auth import UserRead

router = new_router()


@router.get("/users/{username}", response_model=UserRead)
def get_user(username: str, ctx: Ctx) -> UserRead:
    """Return one user (the username is case-insensitive)."""
    with ctx.sessionmaker() as session:
        user = users_db.get_user_by_username(session, username)
        if user is None:
            raise not_found(username)
        return UserRead.model_validate(user)

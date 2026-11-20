"""``GET /users``: every user, ordered by username."""

from __future__ import annotations

from heliopuddle.server.api.deps import Ctx
from heliopuddle.server.api.users._shared import new_router
from heliopuddle.server.models import users as users_db
from heliopuddle.server.schemas.auth import UserRead

router = new_router()


@router.get("/users", response_model=list[UserRead])
def list_users(ctx: Ctx) -> list[UserRead]:
    """Return every user, ordered by username (never a password hash)."""
    with ctx.sessionmaker() as session:
        return [UserRead.model_validate(u) for u in users_db.list_users(session)]

"""``DELETE /users/{username}``: delete a user."""

from __future__ import annotations

from fastapi import HTTPException, Response

from heliopuddle.server.api.deps import Ctx
from heliopuddle.server.api.users._shared import new_router, not_found
from heliopuddle.server.models import users as users_db

router = new_router()


@router.delete(
    "/users/{username}",
    status_code=204,
    responses={409: {"description": "The user is the last active hp-admin"}},
)
def delete_user(username: str, ctx: Ctx) -> Response:
    """Delete a user. The last active ``hp-admin`` cannot be deleted (``409``)."""
    try:
        with ctx.write_session() as session:
            users_db.delete_user(session, username)
    except users_db.UserNotFoundError:
        raise not_found(username) from None
    except users_db.LastAdminError as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    return Response(status_code=204)

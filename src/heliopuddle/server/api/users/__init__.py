"""``/users``: local user management (AUTH-13; ``local`` mode, global ``hp-admin``).

One module per endpoint; each defines a ``router`` that is included here in route
registration order.
"""

from fastapi import APIRouter

from heliopuddle.server.api.users import (
    create_user,
    delete_user,
    get_user,
    list_users,
    update_user,
)

router = APIRouter(tags=["users"])
router.include_router(list_users.router)
router.include_router(create_user.router)
router.include_router(get_user.router)
router.include_router(update_user.router)
router.include_router(delete_user.router)

__all__ = ["router"]

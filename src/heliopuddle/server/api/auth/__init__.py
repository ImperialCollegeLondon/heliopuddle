"""``/auth/*``: token issue, discovery and self-service (AUTH-04, AUTH-11).

One module per endpoint; each defines a ``router`` that is included here in
route registration order.
"""

from fastapi import APIRouter

from heliopuddle.server.api.auth import config, me, password, token

router = APIRouter(tags=["auth"])
router.include_router(token.router)
router.include_router(config.router)
router.include_router(me.router)
router.include_router(password.router)

__all__ = ["router"]

"""``GET /health`` and ``GET /version`` (API-06).

One module per endpoint; each defines a ``router`` that is included here in
route registration order.
"""

from fastapi import APIRouter

from heliopuddle.server.api.health import check, version

router = APIRouter(tags=["health"])
router.include_router(check.router)
router.include_router(version.router)

__all__ = ["router"]

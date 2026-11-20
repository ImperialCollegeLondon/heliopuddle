"""Validated read-only rules and mutable runtime controls (API-14).

One module per endpoint; each defines a ``router`` that is included here in
route registration order.
"""

from fastapi import APIRouter

from heliopuddle.server.api.rules import disable, enable, get, list_rules, trigger

router = APIRouter(tags=["rules"])
router.include_router(list_rules.router)
router.include_router(get.router)
router.include_router(enable.router)
router.include_router(disable.router)
router.include_router(trigger.router)

__all__ = ["router"]

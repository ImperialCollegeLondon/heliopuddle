"""Uploads: reserve, content, renew, complete and abort (STO-09, STO-10).

See docs/design/operations.md ("Add a file", "Overwrite"). A reservation owns
the path in one storage location until it completes, is aborted, or expires.

One module per endpoint; each defines a ``router`` that is included here in
route registration order.
"""

from fastapi import APIRouter

from heliopuddle.server.api.uploads import abort, complete, content, renew, reserve

router = APIRouter(tags=["uploads"])
router.include_router(reserve.router)
router.include_router(content.router)
router.include_router(renew.router)
router.include_router(complete.router)
router.include_router(abort.router)

__all__ = ["router"]

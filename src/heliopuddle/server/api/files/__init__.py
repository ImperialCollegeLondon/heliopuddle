"""Files and series: register, check, query, content, preview, delete, accept, metadata,
content date.

One module per endpoint; each defines a ``router`` that is included here in
route registration order.
"""

from fastapi import APIRouter

from heliopuddle.server.api.files import (
    accept,
    by_path,
    check,
    content,
    content_date,
    delete,
    get,
    list_files,
    metadata,
    preview,
    register,
    rules_ignore,
    series_versions,
)

router = APIRouter(tags=["files"])
router.include_router(register.router)
router.include_router(check.router)
router.include_router(by_path.router)
router.include_router(list_files.router)
router.include_router(get.router)
router.include_router(content.router)
router.include_router(preview.router)
router.include_router(delete.router)
router.include_router(accept.router)
router.include_router(metadata.router)
router.include_router(content_date.router)
router.include_router(rules_ignore.router)
router.include_router(series_versions.router)

__all__ = ["router"]

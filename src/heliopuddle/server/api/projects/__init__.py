"""Projects and storage locations: read-only views of config (API-03, API-05, API-10).

One module per endpoint; each defines a ``router`` that is included here in
route registration order.
"""

from fastapi import APIRouter

from heliopuddle.server.api.projects import (
    get_project,
    get_storage_location,
    list_projects,
    list_storage_locations,
)

router = APIRouter(tags=["projects"])
router.include_router(list_projects.router)
router.include_router(get_project.router)
router.include_router(list_storage_locations.router)
router.include_router(get_storage_location.router)

__all__ = ["router"]

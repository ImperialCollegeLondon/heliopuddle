"""Cross-project file search and project summaries (API-16, API-17).

One module per endpoint; each defines a ``router`` that is included here in
route registration order.
"""

from fastapi import APIRouter

from heliopuddle.server.api.search import project_summary, search_files

router = APIRouter(tags=["search"])
router.include_router(search_files.router)
router.include_router(project_summary.router)

__all__ = ["router"]

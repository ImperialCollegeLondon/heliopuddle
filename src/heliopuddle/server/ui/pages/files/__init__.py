"""The Files pages: list, inspector, file page, preview, content and file actions.

One module per concern, each with its own router; this package puts them in
route order (the inspector and preview paths sit under the file page's).

* ``listing``: ``/projects/{p}/files`` with filters and paging
* ``inspector``: the inspector fragment
* ``detail``: the full file page
* ``fragment``: the content preview fragment (UI-15)
* ``content``: the raw image and the download
* ``actions``: accept and ignore-from-rules
* ``filters``, ``tabs``, ``paging``: parsing and view helpers of the list
* ``preview``: Markdown, JSON colouring and table captions of the preview
"""

from fastapi import APIRouter
from jinja2 import Environment

from heliopuddle.server.ui.pages.files import actions, content, detail, fragment, inspector, listing
from heliopuddle.server.ui.pages.files import preview as ui_preview
from heliopuddle.server.ui.pages.files.lookup import copy_map
from heliopuddle.server.ui.pages.files.paging import PAGE_SIZES

router = APIRouter()
for _module in (listing, inspector, detail, fragment, content, actions):
    router.include_router(_module.router)

__all__ = ["install", "router"]


def install(env: Environment) -> None:
    """Add the filters and globals the Files templates use.

    Args:
        env: the Jinja2 environment.
    """
    env.globals["page_sizes"] = PAGE_SIZES
    env.filters["copy_map"] = copy_map
    env.filters["markdown"] = ui_preview.render_markdown
    env.filters["json_html"] = ui_preview.highlight_json
    env.filters["cell"] = ui_preview.cell_text
    env.filters["rows_caption"] = ui_preview.rows_caption
    env.filters["shape"] = ui_preview.shape_text
    env.globals["numeric_columns"] = ui_preview.numeric_columns
    env.globals["table_title"] = ui_preview.table_title

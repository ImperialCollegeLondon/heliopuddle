"""The Rules page (``/projects/{project}/rules``) and its actions.

* ``page``: the page
* ``actions``: enable, disable, run and run-once
* ``one_off``: the one-off form read into a rule spec
* ``flow``: the data-flow diagram's layout
"""

from fastapi import APIRouter

from heliopuddle.server.ui.pages.rules import actions, page

router = APIRouter()
for _module in (page, actions):
    router.include_router(_module.router)

__all__ = ["router"]

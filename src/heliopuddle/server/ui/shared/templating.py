"""The Jinja2 environment: where templates are found and the filters every page can use.

Templates are co-located with their page: ``pages/files/files.html`` is
``files/files.html`` to Jinja. The loader searches the shared ``templates/``
folder (``base.html``, ``_macros.html``, ``error.html``), then ``pages/``, then
``auth/`` (``login.html``). A page that needs filters of its own adds them from
its ``install`` function (see :func:`heliopuddle.server.ui.pages.files.install`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context

from heliopuddle.server.schemas import FileState
from heliopuddle.server.ui.shared import fileinfo, format, rules_text, states

UI_DIR = Path(__file__).resolve().parents[1]
TEMPLATE_DIRS = [UI_DIR / "templates", UI_DIR / "pages", UI_DIR / "auth"]


@pass_context
def path_for(context: dict[str, Any], name: str, /, **params: Any) -> str:
    """Template global: the root-relative path of a named UI route."""
    request: Request = context["request"]
    return request.url_for(name, **params).path


def make_templates() -> Jinja2Templates:
    """Build the templates with the shared filters, tests and globals registered.

    Returns:
        The Jinja2 templates; pages add their own filters to ``templates.env``.
    """
    templates = Jinja2Templates(directory=TEMPLATE_DIRS)
    env = templates.env
    env.globals["path_for"] = path_for
    env.globals["file_states"] = list(FileState)
    env.globals["state_order"] = states.STATE_ORDER
    env.globals["state_style"] = states.state_style
    env.filters["human_size"] = format.human_size
    env.filters["number"] = format.number
    env.filters["datetime"] = format.format_datetime
    env.filters["date"] = format.format_date
    env.filters["ago"] = format.relative_time
    env.filters["version_label"] = fileinfo.version_label
    env.filters["metadata_value"] = format.metadata_value
    env.filters["split_path"] = format.split_path
    env.filters["initials"] = format.initials
    env.filters["scheme"] = format.scheme
    env.filters["health"] = fileinfo.file_health
    env.filters["rule_route"] = rules_text.rule_route
    env.filters["rule_match"] = rules_text.rule_match_summary
    env.filters["rule_summary"] = rules_text.rule_full_summary
    env.tests["latest_eligible"] = fileinfo.latest_eligible
    env.tests["downloadable"] = fileinfo.downloadable
    return templates

"""The projects home page: a card per readable project and a "Needs attention" list."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from heliopuddle.client import HelioPuddleError
from heliopuddle.server.ui.pages.home.health import project_health
from heliopuddle.server.ui.shared.context import Ui
from heliopuddle.server.ui.shared.summary import AttentionItem, attention_items

router = APIRouter()


@router.get("/", response_class=HTMLResponse, name="ui_index")
def index(request: Request, ui: Ui) -> Response:
    """List the readable projects; a single one redirects to its Overview."""
    with ui.client_for(request, None) as client:
        projects = client.list_projects()
        if len(projects) == 1:
            return RedirectResponse(
                ui.url(request, "ui_overview", project=projects[0].name), status_code=303
            )
        cards: list[dict[str, Any]] = []
        attention: list[AttentionItem] = []
        for p in projects:
            try:
                summary = client.project_summary(p.name)
            except HelioPuddleError as exc:
                if exc.status_code == 401:
                    raise
                cards.append({"name": p.name, "summary": None, "sls": p.storage_locations})
                continue
            badge, tone = project_health(summary)
            cards.append(
                {
                    "name": p.name,
                    "summary": summary,
                    "badge": badge,
                    "tone": tone,
                    "sls": [s.name for s in summary.storage_locations],
                }
            )
            attention.extend(attention_items(summary, ui.files_link(request, p.name)))
    summaries = [c["summary"] for c in cards if c["summary"] is not None]
    return ui.render(
        request,
        "home/home.html",
        cards=cards,
        attention=attention,
        projects=[p.name for p in projects],
        totals={
            "files": sum(s.files for s in summaries),
            "bytes": sum(s.bytes for s in summaries),
            "attention": sum(1 for s in summaries if s.views.needs_attention),
        },
    )

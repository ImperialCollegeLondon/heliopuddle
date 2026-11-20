"""The project Overview and its "Reconcile now" action."""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from heliopuddle.server.ui.pages.overview.heatmap import heatmap
from heliopuddle.server.ui.shared.context import Ui
from heliopuddle.server.ui.shared.forms import FormLists, SameOrigin, form_bool
from heliopuddle.server.ui.shared.summary import attention_items, view_counts

router = APIRouter()

RECENT_FILES = 6


@router.get("/projects/{project}", response_class=HTMLResponse, name="ui_overview")
def overview(request: Request, ui: Ui, project: str, reconcile_run: str | None = None) -> Response:
    """Key figures, the copy-state heatmap, needs attention, rules and recent files."""
    with ui.client_for(request, project) as client:
        summary = client.project_summary()
        rules = client.list_rules()
        recent = client.find(limit=RECENT_FILES)
        shell = ui.project_shell(request, client, project, summary, rules)
    link = ui.files_link(request, project)
    return ui.render(
        request,
        "overview/overview.html",
        project=project,
        tab="overview",
        **shell,
        rules=rules,
        recent=recent.items[:RECENT_FILES],
        heat=heatmap(summary.storage_locations, lambda n, s: link(None, n, s)),
        attention=attention_items(summary, lambda v, n, s: link(v, n, s), include_progress=True),
        counts=view_counts(summary),
        notice=ui.run_notice(reconcile_run, "reconcile"),
    )


@router.post("/projects/{project}/reconcile", name="ui_reconcile_now", dependencies=[SameOrigin])
def reconcile_now(
    request: Request, ui: Ui, project: str, form: dict[str, list[str]] = FormLists
) -> Response:
    """Start a reconcile run and redirect to the Overview with its notice."""
    verify = form_bool(form.get("verify", [""])[0], "verify")
    # The select lists every rule; when it was offered, what is selected is the
    # choice (all of them sends None, none of them sends an empty list).
    rules: list[str] | None = None
    if form.get("has_rules"):
        rules = form.get("rules", [])
    with ui.client_for(request, project) as client:
        if rules is not None and set(rules) == {r.name for r in client.list_rules()}:
            rules = None
        run = client.reconcile(verify=verify, rules=rules)
    target = ui.url(request, "ui_overview", project=project)
    return RedirectResponse(
        f"{target}?{urlencode({'reconcile_run': run.flow_run_id})}", status_code=303
    )

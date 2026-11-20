"""The Storage pages: the list, one storage location, and the "Index now" action."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from heliopuddle.server.schemas import ProjectSummary, RuleRead
from heliopuddle.server.ui.pages.storage.bar import bar_segments
from heliopuddle.server.ui.shared.context import Ui, UiContext
from heliopuddle.server.ui.shared.errors import BadRequestError
from heliopuddle.server.ui.shared.format import scheme
from heliopuddle.server.ui.shared.forms import RUN_MODES, FormData, SameOrigin

router = APIRouter()


def storage_context(
    ui: UiContext, request: Request, project: str
) -> tuple[dict[str, Any], list[Any], list[RuleRead], ProjectSummary]:
    """What both storage pages need: the shell, an item per storage location, rules, summary."""
    with ui.client_for(request, project) as client:
        summary = client.project_summary()
        details = client.list_storage_locations()
        rules = client.list_rules()
        shell = ui.project_shell(request, client, project, summary, rules)
    by_name = {s.name: s for s in summary.storage_locations}
    link = ui.files_link(request, project)
    items = [
        {
            "read": sl,
            "summary": by_name.get(sl.name),
            "kind": scheme(sl.url),
            "bar": bar_segments(by_name[sl.name], lambda n, s: link(None, n, s))
            if sl.name in by_name
            else [],
        }
        for sl in details
    ]
    return shell, items, rules, summary


@router.get("/projects/{project}/storage", response_class=HTMLResponse, name="ui_storage")
def storage_page(request: Request, ui: Ui, project: str) -> Response:
    """The project's storage locations with their copy-state bars."""
    shell, items, rules, _ = storage_context(ui, request, project)
    return ui.render(
        request, "storage/storage.html", project=project, tab="storage", **shell, items=items,
        rules=rules,
    )  # fmt: skip


@router.get(
    "/projects/{project}/storage/{name}",
    response_class=HTMLResponse,
    name="ui_storage_location",
)
def storage_location_page(
    request: Request,
    ui: Ui,
    project: str,
    name: str,
    index_run: str | None = None,
    index_mode: str | None = None,
) -> Response:
    """One storage location: config, index times and buttons, bar, rules touching it."""
    shell, items, rules, _ = storage_context(ui, request, project)
    item = next((i for i in items if i["read"].name == name), None)
    if item is None:
        return ui.error_page(
            request, 404, "Error 404", f"Storage location {name!r} not found in {project!r}."
        )
    touching = [
        {"rule": r, "direction": "in" if r.destination == name else "out"}
        for r in rules
        if r.source == name or r.destination == name
    ]
    return ui.render(
        request,
        "storage/storage_location.html",
        project=project,
        tab="storage",
        **shell,
        items=items,
        item=item,
        touching=touching,
        files_url=ui.files_link(request, project)(None, name),
        notice=ui.run_notice(
            index_run, "index", mode=index_mode if index_mode in RUN_MODES else None
        ),
    )


@router.post(
    "/projects/{project}/storage/{storage_location}/index",
    name="ui_index_now",
    dependencies=[SameOrigin],
)
def index_now(
    request: Request, ui: Ui, project: str, storage_location: str, form: dict[str, str] = FormData
) -> Response:
    """Start an index run for a storage location and redirect to its page."""
    mode = form.get("mode", "full")
    if mode not in RUN_MODES:
        raise BadRequestError(f"Unknown index mode {mode!r}.")
    with ui.client_for(request, project) as client:
        run = client.trigger_index(
            storage_location, mode="full" if mode == "full" else "incremental"
        )
    target = ui.url(request, "ui_storage_location", project=project, name=storage_location)
    params = {"index_run": run.flow_run_id, "index_mode": mode}
    return RedirectResponse(f"{target}?{urlencode(params)}", status_code=303)

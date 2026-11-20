"""Rule actions: enable, disable, run, and run a one-off rule (UI-04).

Each answers with a redirect to the Rules page, with a "run submitted" notice
for the runs.
"""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from heliopuddle.server.ui.pages.rules.one_off import one_off_spec
from heliopuddle.server.ui.shared.context import Ui
from heliopuddle.server.ui.shared.errors import BadRequestError
from heliopuddle.server.ui.shared.forms import RUN_MODES, FormData, SameOrigin, form_bool

router = APIRouter()


@router.post(
    "/projects/{project}/rules/{name}/enable", name="ui_enable_rule", dependencies=[SameOrigin]
)
def enable_rule(request: Request, ui: Ui, project: str, name: str) -> Response:
    """Enable a rule that is switched off."""
    with ui.client_for(request, project) as client:
        client.enable_rule(name)
    return RedirectResponse(ui.url(request, "ui_rules", project=project), status_code=303)


@router.post(
    "/projects/{project}/rules/{name}/disable",
    name="ui_disable_rule",
    dependencies=[SameOrigin],
)
def disable_rule(request: Request, ui: Ui, project: str, name: str) -> Response:
    """Disable a rule."""
    with ui.client_for(request, project) as client:
        client.disable_rule(name)
    return RedirectResponse(ui.url(request, "ui_rules", project=project), status_code=303)


@router.post("/projects/{project}/rules/{name}/run", name="ui_run_rule", dependencies=[SameOrigin])
def run_rule(
    request: Request, ui: Ui, project: str, name: str, form: dict[str, str] = FormData
) -> Response:
    """Run a configured rule now."""
    mode = form.get("mode", "incremental")
    if mode not in RUN_MODES:
        raise BadRequestError(f"Unknown rule mode {mode!r}.")
    with ui.client_for(request, project) as client:
        run = client.run_rule(
            name,
            mode="full" if mode == "full" else "incremental",
            dry_run=form_bool(form.get("dry_run", ""), "dry_run"),
        )
    target = ui.url(request, "ui_rules", project=project)
    return RedirectResponse(f"{target}?{urlencode({'rule_run': run.flow_run_id})}", status_code=303)


@router.post("/projects/{project}/rules/run-once", name="ui_run_once", dependencies=[SameOrigin])
def run_once(request: Request, ui: Ui, project: str, form: dict[str, str] = FormData) -> Response:
    """Run a rule built from the one-off form."""
    spec = one_off_spec(form)
    with ui.client_for(request, project) as client:
        run = client.run_once(spec)
    target = ui.url(request, "ui_rules", project=project)
    return RedirectResponse(f"{target}?{urlencode({'rule_run': run.flow_run_id})}", status_code=303)

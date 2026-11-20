"""The Rules page: data-flow diagram, rules table and the one-off run form (UI-04)."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from heliopuddle.config.models import RuleMatch, RuleOptions
from heliopuddle.server.schemas import RuleRead
from heliopuddle.server.ui.pages.rules.flow import layout_flow
from heliopuddle.server.ui.shared.context import Ui
from heliopuddle.server.ui.shared.format import scheme
from heliopuddle.settings import Settings, prefect_ui_url

router = APIRouter()

_CONFIG_EXCLUDE = {
    "effective_enabled",
    "deployment",
    "deployment_id",
    "deployment_url",
    "watermark_at",
    "watermark_id",
    "last_success_at",
}


def _deployment_url(rule: RuleRead, settings: Settings) -> str | None:
    base = prefect_ui_url(settings)
    if base and rule.deployment_id:
        try:
            deployment_id = UUID(rule.deployment_id)
        except ValueError:
            return None
        return f"{base}/deployments/deployment/{deployment_id}"
    return rule.deployment_url


@router.get("/projects/{project}/rules", name="ui_rules", response_class=HTMLResponse)
def rules_page(request: Request, ui: Ui, project: str, rule_run: str | None = None) -> Response:
    """The project's rules with enable/disable and Run controls."""
    with ui.client_for(request, project) as client:
        summary = client.project_summary()
        rules = client.list_rules()
        details = client.list_storage_locations()
        shell = ui.project_shell(request, client, project, summary, rules)
    kinds = {s.name: scheme(s.url) for s in details}
    return ui.render(
        request,
        "rules/rules.html",
        project=project,
        tab="rules",
        **shell,
        rules=rules,
        flow=layout_flow(summary.storage_locations, rules, kinds),
        storage_locations=[s for s in summary.storage_locations if s.active],
        deployment_urls={rule.name: _deployment_url(rule, ui.settings) for rule in rules},
        configs={
            rule.name: rule.model_dump(mode="json", exclude=_CONFIG_EXCLUDE) for rule in rules
        },
        option_defaults=RuleOptions().model_dump(mode="json"),
        match_defaults=RuleMatch(paths=["**"]).model_dump(mode="json"),
        notice=ui.run_notice(rule_run, "rule"),
    )

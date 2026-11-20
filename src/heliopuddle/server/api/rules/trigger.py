"""``POST /projects/{project}/rules/{name}/trigger``."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from heliopuddle.server.api.deps import Ctx, require_project, requires
from heliopuddle.server.api.errors import rule_errors
from heliopuddle.server.auth.roles import ROLE_OPERATOR
from heliopuddle.server.schemas import RuleTriggerResult
from heliopuddle.server.services import rule_control

router = APIRouter()


@router.post(
    "/projects/{project}/rules/{name}/trigger",
    response_model=RuleTriggerResult,
    dependencies=[Depends(requires(ROLE_OPERATOR))],
)
def trigger_rule(project: str, name: str, ctx: Ctx) -> RuleTriggerResult:
    """Trigger incremental work, or successfully report an existing unfinished run."""
    require_project(ctx, project)
    with rule_errors():
        return rule_control.trigger_rule(ctx, project, name)

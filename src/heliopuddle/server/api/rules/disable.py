"""``POST /projects/{project}/rules/{name}/disable``."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from heliopuddle.server.api.deps import Ctx, require_project, requires
from heliopuddle.server.api.errors import rule_errors
from heliopuddle.server.auth.roles import ROLE_OPERATOR
from heliopuddle.server.schemas import RuleRead
from heliopuddle.server.services import rule_control

router = APIRouter()


@router.post(
    "/projects/{project}/rules/{name}/disable",
    response_model=RuleRead,
    dependencies=[Depends(requires(ROLE_OPERATOR))],
)
def disable_rule(project: str, name: str, ctx: Ctx) -> RuleRead:
    """Disable the runtime switch, pause schedules and cancel future runs."""
    require_project(ctx, project)
    with rule_errors():
        return rule_control.set_rule_enabled(ctx, project, name, False)

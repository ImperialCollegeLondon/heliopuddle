"""``GET /projects/{project}/rules``: rules with their runtime state (API-14)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from heliopuddle.server.api.deps import Ctx, require_project, requires
from heliopuddle.server.api.errors import rule_errors
from heliopuddle.server.auth.roles import ROLE_READER
from heliopuddle.server.schemas import RuleRead
from heliopuddle.server.services import rule_control

router = APIRouter()


@router.get(
    "/projects/{project}/rules",
    response_model=list[RuleRead],
    dependencies=[Depends(requires(ROLE_READER))],
)
def list_rules(project: str, ctx: Ctx) -> list[RuleRead]:
    """Return rules with validated defaults, effective switches and progress."""
    require_project(ctx, project)
    with rule_errors():
        return rule_control.list_rules(ctx, project)

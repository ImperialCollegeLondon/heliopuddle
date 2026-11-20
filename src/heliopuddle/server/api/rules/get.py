"""``GET /projects/{project}/rules/{name}``: one rule."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from heliopuddle.server.api.deps import Ctx, require_project, requires
from heliopuddle.server.api.errors import rule_errors
from heliopuddle.server.auth.roles import ROLE_READER
from heliopuddle.server.schemas import RuleRead
from heliopuddle.server.services import rule_control

router = APIRouter()


@router.get(
    "/projects/{project}/rules/{name}",
    response_model=RuleRead,
    dependencies=[Depends(requires(ROLE_READER))],
)
def get_rule(project: str, name: str, ctx: Ctx) -> RuleRead:
    """Return one immutable rule configuration and its runtime state."""
    require_project(ctx, project)
    with rule_errors():
        return rule_control.get_rule(ctx, project, name)

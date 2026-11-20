"""Read a configured rule together with its persisted runtime state (SVC-24)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from heliopuddle.config.models import RuleConfig
from heliopuddle.server import models
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import RuleState
from heliopuddle.server.schemas import RuleRead
from heliopuddle.server.services.rule_prefect import RuleDisabledError


def _configured(ctx: AppContext, project: str, name: str) -> RuleConfig:
    try:
        return ctx.projects[project].rule(name)
    except KeyError:
        raise models.NotFoundError(f"rule {name!r} not found in project {project!r}") from None


def _project_id(session: Session, project: str) -> int:
    row = models.get_project(session, project)
    if row is None:
        raise models.NotFoundError(f"project {project!r} not found")
    return row.id


def _read(cfg: RuleConfig, project: str, project_id: int, state: RuleState | None) -> RuleRead:
    from heliopuddle.server.services.rule_names import rule_deployment_full_name

    return RuleRead(
        **cfg.model_dump(),
        effective_enabled=cfg.enabled and (state is None or state.enabled),
        deployment=rule_deployment_full_name(project_id, project, cfg.name),
        watermark_at=state.watermark_at if state else None,
        watermark_id=state.watermark_id if state else None,
        last_success_at=state.last_success_at if state else None,
    )


def _enabled(cfg: RuleConfig, state: RuleState | None, project: str) -> None:
    if not cfg.enabled or (state is not None and not state.enabled):
        raise RuleDisabledError(f"rule {cfg.name!r} in project {project!r} is disabled")


def _snapshot(
    ctx: AppContext, project: str, name: str, *, require_enabled: bool = False
) -> tuple[RuleConfig, int, RuleRead]:
    cfg = _configured(ctx, project, name)
    with ctx.sessionmaker() as session:
        project_id = _project_id(session, project)
        state = session.get(RuleState, (project_id, name))
        if require_enabled:
            _enabled(cfg, state, project)
        return cfg, project_id, _read(cfg, project, project_id, state)

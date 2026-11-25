"""Rule run types: the internal checkpoint, the run summary and a resolved rule (M4).

See docs/design/rules.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from heliopuddle.config.models import RuleConfig, RuleSpec
from heliopuddle.server import models
from heliopuddle.server.context import AppContext
from heliopuddle.server.models import rules as rule_db
from heliopuddle.server.schemas.rules import RuleCursor

RuleMode = Literal["full", "incremental"]
SYSTEM_USER = "heliopuddle:rule"


class RuleContinuation(BaseModel):
    """Internal Prefect checkpoint; not a public configured-rule override."""

    model_config = ConfigDict(extra="forbid")

    chain_id: str = Field(default_factory=lambda: str(uuid4()))
    mode: RuleMode
    selected_at: datetime
    changed_after: RuleCursor | None = None
    age_since: datetime | None = None
    state_success_at: datetime | None = None
    cursor: RuleCursor | None = None
    full_after_id: int | None = None
    watermark: RuleCursor | None = None
    pending: bool = True
    spec_hash: str = ""


class RuleSummary(BaseModel):
    """Counts returned by a rule flow; details are logged, never published as artifacts."""

    project: str
    rule: str | None = None
    mode: RuleMode = "full"
    processed: int = 0
    bytes_processed: int = 0
    outcomes: dict[str, int] = Field(default_factory=dict)
    remaining: bool = False
    disabled: bool = False


@dataclass(frozen=True)
class _Rule:
    project: str
    project_id: int
    source_id: int
    spec: RuleSpec
    name: str | None = None
    # The destination's path_prefix (STO-23): files outside it are never selected.
    within: str = ""


def _resolve(ctx: AppContext, project: str, spec: RuleSpec, name: str | None = None) -> _Rule:
    cfg = ctx.projects.get(project)
    if cfg is None:
        raise models.NotFoundError(f"project {project!r} is not in config")
    cfg.validate_rule(spec)
    with ctx.sessionmaker() as session:
        source = models.get_storage_location(session, project, spec.source)
        if source is None or not source.active:
            raise models.NotFoundError(f"storage location {project}/{spec.source} is inactive")
        within = cfg.storage_location(spec.destination).path_prefix if spec.destination else ""
        return _Rule(project, source.project_id, source.id, spec, name, within)


def _enabled(ctx: AppContext, rule: _Rule) -> bool:
    if rule.name is None:
        return True
    with ctx.sessionmaker() as session:
        return rule_db.effective_enabled(
            session,
            rule.project_id,
            rule.name,
            rule.spec.enabled if isinstance(rule.spec, RuleConfig) else True,
        )

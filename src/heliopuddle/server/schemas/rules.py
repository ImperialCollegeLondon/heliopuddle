"""Rule control schemas shared with the HTTP-only client."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from heliopuddle.config.models import RuleConfig


class RuleRead(RuleConfig):
    """Validated rule config with effective enablement and runtime progress."""

    effective_enabled: bool
    deployment: str
    deployment_id: str | None = None
    deployment_url: str | None = None
    watermark_at: datetime | None = None
    watermark_id: int | None = None
    last_success_at: datetime | None = None


class RuleTriggerResult(BaseModel):
    """A trigger either submits a run or successfully finds existing work."""

    started: bool
    flow_run_id: str | None = None


class RulesIgnoreRequest(BaseModel):
    """Set a file's exclusion from every archiving rule."""

    model_config = ConfigDict(extra="forbid")

    ignored: bool


class RuleCursor(BaseModel):
    """A stable keyset boundary including files tied on update timestamp."""

    model_config = ConfigDict(extra="forbid")

    updated_at: datetime
    id: int = Field(ge=0)

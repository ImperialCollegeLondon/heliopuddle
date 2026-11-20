"""Run submission schemas.

This module must not import SQLAlchemy or anything from ``heliopuddle.server``
other than the schema modules: the client imports it.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from heliopuddle.config.models import RuleSpec


class RunRequest(BaseModel):
    """The index variant of ``POST /projects/{p}/runs``."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["index"]
    storage_location: str | None = Field(
        default=None, description="Defaults to the project's first storage location"
    )
    mode: Literal["full", "incremental"] = "full"


class RuleRunRequest(BaseModel):
    """Run a configured rule without overriding its validated configuration."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["rule"]
    rule: str = Field(min_length=1)
    mode: Literal["full", "incremental"] = "incremental"
    dry_run: bool = False


class RuleOnceRunRequest(BaseModel):
    """Run one complete, unsaved specification as a single full batch."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["rule_once"]
    spec: RuleSpec


class ReconcileRunRequest(BaseModel):
    """Reconcile the project (M5): index, optional verify, then rules to a fixed point."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["reconcile"]
    verify: bool = Field(default=False, description="Also re-hash every available copy")
    rules: list[str] | None = Field(
        default=None, description="Only these rules in the rule phase; None means all"
    )


RunSubmission = Annotated[
    RunRequest | RuleRunRequest | RuleOnceRunRequest | ReconcileRunRequest,
    Field(discriminator="kind"),
]


class RunInfo(BaseModel):
    """A submitted Prefect flow run."""

    flow_run_id: str
    flow_run_url: str | None = None
    deployment: str

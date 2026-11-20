"""Project configuration: storage locations, index and reconcile settings, the project."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from heliopuddle.config.models.common import NAME_PATTERN, BatchConfig, Size, validate_cron
from heliopuddle.config.models.rules import RuleConfig, RuleSpec
from heliopuddle.config.models.versioning import DEFAULT_VERSION_RULES, VersionRule
from heliopuddle.durations import Duration


class StorageLocationConfig(BaseModel):
    """One storage location of a project."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=NAME_PATTERN)
    url: str = Field(
        min_length=1,
        description="fsspec URL of the root, e.g. file:///data or <protocol>://bucket/prefix",
    )
    storage_options: dict[str, Any] = Field(default_factory=dict)
    accept_external_changes: bool = True
    create_root: bool | Literal["auto"] = Field(
        default="auto",
        description="Create a missing root (true), never (false), or auto: create it except "
        "on object stores, whose bucket or container must already exist",
    )
    quota: Size | None = Field(default=None, gt=0, description="Optional indexed-size quota")


_PREFIX_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
_PREFIX_PROBE_DATE = datetime(2000, 1, 2, 3, 4, 5, tzinfo=UTC)


class IndexConfig(BaseModel):
    """Project ``index:`` settings. See docs/design/indexing.md."""

    model_config = ConfigDict(extra="forbid")

    full_schedule: str | None = Field(default=None, description="Cron for index_full")
    incremental_schedule: str | None = Field(default=None, description="Cron for index_incremental")
    batch_size: int = Field(default=1, ge=1, description="Listed entries per index_batch task")
    batch: BatchConfig = Field(default_factory=BatchConfig)
    retrigger: bool = True
    pause_between_runs: Duration = Field(default=timedelta(seconds=30), ge=timedelta())
    settle_time: Duration = Field(default=timedelta(seconds=60))
    reverify_after: Duration = Field(default=timedelta(hours=1))
    lock_timeout: Duration = Field(default=timedelta(hours=1))
    incremental_overlap: Duration = Field(default=timedelta(minutes=5))
    incremental_lookback: Duration = Field(default=timedelta(days=2))
    incremental_prefixes: list[str] = Field(
        default_factory=list, description="strftime templates such as science/{date:%Y/%m/%d}"
    )

    @field_validator("full_schedule", "incremental_schedule")
    @classmethod
    def _cron(cls, v: str | None) -> str | None:
        return validate_cron(v)

    @field_validator("incremental_prefixes")
    @classmethod
    def _prefixes(cls, v: list[str]) -> list[str]:
        for template in v:
            for placeholder in _PREFIX_PLACEHOLDER.findall(template):
                if not placeholder.startswith("date:") or len(placeholder) == len("date:"):
                    raise ValueError(
                        f"{template!r}: only {{date:<strftime>}} placeholders are allowed"
                    )
            if template.startswith("/") or ".." in template.split("/"):
                raise ValueError(f"{template!r}: must be a relative path")
            # Format it once now (format syntax and strftime), so a template
            # the indexer would fail on is rejected at load time (DEP-12).
            try:
                probe = template.format(date=_PREFIX_PROBE_DATE)
            except (ValueError, KeyError, IndexError, AttributeError) as e:
                raise ValueError(f"{template!r}: invalid template: {e}") from None
            if probe.startswith("/") or ".." in probe.split("/"):
                raise ValueError(f"{template!r}: must expand to a relative path, got {probe!r}")
        return v


class ReconcileConfig(BaseModel):
    """Project ``reconcile:`` settings (M5). See docs/design/rules.md#reconcile-m5."""

    model_config = ConfigDict(extra="forbid")

    schedule: str | None = Field(default=None, description="Cron for scheduled reconciles")
    verify: bool = Field(default=False, description="Re-hash available copies (scheduled runs)")
    rules: list[str] | None = Field(
        default=None, description="Rules to reconcile (scheduled runs); None means all"
    )
    max_rule_passes: int = Field(default=3, ge=1, description="Rule passes to reach a fixed point")
    max_failures: int = Field(default=5, ge=1, description="File failures before a pass stops")

    @field_validator("schedule")
    @classmethod
    def _cron(cls, v: str | None) -> str | None:
        return validate_cron(v)


class ProjectConfig(BaseModel):
    """A project: a named set of files stored in one or more storage locations."""

    model_config = ConfigDict(extra="allow")

    project: str = Field(pattern=NAME_PATTERN)
    storage_locations: list[StorageLocationConfig] = Field(min_length=1)
    index: IndexConfig = Field(default_factory=IndexConfig)
    versioning: list[VersionRule] = Field(
        default_factory=lambda: list(DEFAULT_VERSION_RULES),
        description="Version rules; first match wins. Unset: one default number rule "
        "for names like file_v12.csv; [] turns versioning off",
    )
    rules: list[RuleConfig] = Field(default_factory=list)
    reconcile: ReconcileConfig = Field(default_factory=ReconcileConfig)

    @field_validator("storage_locations")
    @classmethod
    def _unique_names(cls, v: list[StorageLocationConfig]) -> list[StorageLocationConfig]:
        names = [s.name for s in v]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate storage location names: {', '.join(dupes)}")
        return v

    @field_validator("rules")
    @classmethod
    def _unique_rules(cls, rules: list[RuleConfig]) -> list[RuleConfig]:
        names = [rule.name for rule in rules]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate rule names: {', '.join(duplicates)}")
        return rules

    @model_validator(mode="after")
    def _known_extras(self) -> ProjectConfig:
        allowed = {"analysis"}
        unknown = set((self.model_extra or {}).keys()) - allowed
        if unknown:
            raise ValueError(f"unknown keys: {', '.join(sorted(unknown))}")
        for index, rule in enumerate(self.rules):
            try:
                self.validate_rule(rule)
            except ValueError as error:
                raise ValueError(f"rules.{index}: {error}") from error
        self.check_rule_names(self.reconcile.rules, field="reconcile.rules")
        return self

    def check_rule_names(self, names: list[str] | None, *, field: str = "rules") -> None:
        """Check that every name in ``names`` is a configured rule of this project.

        Raises:
            ValueError: naming the unknown rules.
        """
        unknown = sorted(set(names or ()) - {rule.name for rule in self.rules})
        if unknown:
            raise ValueError(f"{field}: unknown rule(s): {', '.join(unknown)}")

    def validate_rule(self, spec: RuleSpec) -> None:
        """Validate a configured or one-off rule's storage-location references.

        Raises:
            ValueError: if the source or destination is not in this project.
        """
        names = {location.name for location in self.storage_locations}
        if spec.source not in names:
            raise ValueError(f"source storage location {spec.source!r} does not exist")
        if spec.destination is not None and spec.destination not in names:
            raise ValueError(f"destination storage location {spec.destination!r} does not exist")

    def rule(self, name: str) -> RuleConfig:
        """Return a configured rule by name.

        Raises:
            KeyError: if this project has no rule with that name.
        """
        for rule in self.rules:
            if rule.name == name:
                return rule
        raise KeyError(name)

    def storage_location(self, name: str | None = None) -> StorageLocationConfig:
        """Return the named storage location, or the first one when ``name`` is None.

        Raises:
            KeyError: if there is no storage location with that name.
        """
        if name is None:
            return self.storage_locations[0]
        for s in self.storage_locations:
            if s.name == name:
                return s
        raise KeyError(name)

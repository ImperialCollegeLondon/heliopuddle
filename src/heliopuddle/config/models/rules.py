"""Rule configuration: age criteria, match, options and the rule models."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ByteSize,
    ConfigDict,
    Field,
    PlainSerializer,
    field_validator,
    model_validator,
)

from heliopuddle.config.models.common import NAME_PATTERN, BatchConfig, Size, validate_cron
from heliopuddle.durations import Duration, format_duration, parse_duration
from heliopuddle.globs import glob_to_regex

_AGE_LIMIT_HINT = (
    "use a duration such as 3d, 1h30m or 1y6mo, or an ISO date/time such as 2025-01-01"
)


def _age_limit(value: Any) -> timedelta | datetime:
    """Parse an age bound: a duration back from the run's selection time, or a UTC instant."""
    if isinstance(value, bool):
        raise ValueError(_AGE_LIMIT_HINT)
    if isinstance(value, timedelta):
        return parse_duration(value)
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if isinstance(value, date):
        return datetime.combine(value, time(), tzinfo=UTC)
    if isinstance(value, int | float):
        return parse_duration(value)
    if not isinstance(value, str):
        raise ValueError(_AGE_LIMIT_HINT)
    try:
        return parse_duration(value)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as error:
        raise ValueError(f"invalid age bound {value!r}; {_AGE_LIMIT_HINT}") from error
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _format_age_limit(value: timedelta | datetime) -> str:
    return format_duration(value) if isinstance(value, timedelta) else value.isoformat()


AgeLimit = Annotated[
    timedelta | datetime,
    BeforeValidator(_age_limit),
    PlainSerializer(_format_age_limit, return_type=str),
]
"""An age bound: a non-negative duration back from the run's selection time, or a UTC instant."""

AgeField = Literal["modified", "created", "content_date", "arrived"]
"""The dates a rule can select files by (docs/design/rules.md, "Age criteria")."""

AGE_FIELDS: tuple[AgeField, ...] = ("modified", "created", "content_date", "arrived")


def age_cutoff(limit: timedelta | datetime, now: datetime) -> datetime:
    """Return the instant an age bound stands for at selection time ``now``."""
    return now - limit if isinstance(limit, timedelta) else limit


class AgeRange(BaseModel):
    """Bounds on one date: older than and/or younger than (both: a range).

    ``older_than`` selects dates strictly before its cutoff; ``younger_than``
    dates at or after its cutoff.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    older_than: AgeLimit | None = None
    younger_than: AgeLimit | None = None

    @model_validator(mode="after")
    def _bounded(self) -> AgeRange:
        older, younger = self.older_than, self.younger_than
        if older is None and younger is None:
            raise ValueError("give older_than, younger_than or both")
        if isinstance(older, timedelta) and isinstance(younger, timedelta) and older >= younger:
            raise ValueError(
                f"older_than {format_duration(older)} must be shorter than "
                f"younger_than {format_duration(younger)}, or nothing can match"
            )
        if isinstance(older, datetime) and isinstance(younger, datetime) and older <= younger:
            raise ValueError(
                f"older_than {older.isoformat()} must be later than "
                f"younger_than {younger.isoformat()}, or nothing can match"
            )
        return self


class AgeCriteria(BaseModel):
    """Age criteria on up to four dates; every one given must hold."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    modified: AgeRange | None = Field(
        default=None, description="Source copy mtime, else when hp first recorded the file"
    )
    created: AgeRange | None = Field(default=None, description="When hp first recorded the file")
    content_date: AgeRange | None = Field(
        default=None, description="The file's content date; a missing date never matches"
    )
    arrived: AgeRange | None = Field(
        default=None,
        description="When the file arrived in the source storage location (first recorded "
        "there, or last copied or moved there by a rule)",
    )

    def items(self) -> list[tuple[AgeField, AgeRange]]:
        """The criteria that are set, as ``(field, range)`` in a fixed order."""
        return [(f, r) for f in AGE_FIELDS if (r := getattr(self, f)) is not None]


_REMOVED_MATCH_KEYS = {
    "older_than": "use age: {modified: {older_than: ...}}",
    "age_by": "use age: {content_date: {...}} or age: {modified: {...}}",
    "content_date_after": "use age: {content_date: {younger_than: ...}}",
    "content_date_before": "use age: {content_date: {older_than: ...}}",
}


class RuleMatch(BaseModel):
    """Selection criteria shared by configured rules and unsaved one-off runs."""

    model_config = ConfigDict(extra="forbid")

    paths: list[str] = Field(min_length=1)
    exclude: list[str] = Field(default_factory=list)
    is_latest: bool | None = Field(
        default=None, description="null: every version; true: latest only; false: all but latest"
    )
    age: AgeCriteria = Field(default_factory=AgeCriteria)

    @model_validator(mode="before")
    @classmethod
    def _removed_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for key, hint in _REMOVED_MATCH_KEYS.items():
                if key in data:
                    raise ValueError(f"{key!r} was removed; {hint}")
        return data

    @field_validator("paths", "exclude")
    @classmethod
    def _globs(cls, values: list[str]) -> list[str]:
        for value in values:
            if (
                not value.strip()
                or value.startswith("/")
                or "\\" in value
                or "\x00" in value
                or any(part in {".", ".."} for part in value.split("/"))
            ):
                raise ValueError(f"{value!r}: globs must be non-empty relative paths")
            glob_to_regex(value)
        return values


class RuleOptions(BaseModel):
    """Transfer decisions and bounded-run controls, all with working defaults."""

    model_config = ConfigDict(extra="forbid")

    overwrite: bool = False
    check_for_space: bool = True
    check_for_space_overhead: Size = Field(default=ByteSize(2_000_000_000), ge=0)
    update_only_if_newer: bool = False
    dry_run: bool = False
    retrigger: bool = True
    pause_between_runs: Duration = Field(default=timedelta(seconds=30), ge=timedelta())
    batch: BatchConfig = Field(default_factory=BatchConfig)


class RuleSpec(BaseModel):
    """A complete, unsaved rule specification for a one-off run."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["copy", "move", "delete"]
    source: str = Field(pattern=NAME_PATTERN)
    destination: str | None = Field(default=None, pattern=NAME_PATTERN)
    match: RuleMatch
    options: RuleOptions = Field(default_factory=RuleOptions)

    @model_validator(mode="after")
    def _destination(self) -> RuleSpec:
        if self.action == "delete":
            if self.destination is not None:
                raise ValueError("delete must not have a destination")
        elif self.destination is None:
            raise ValueError("copy and move require a destination")
        if self.source == self.destination:
            raise ValueError("source and destination must differ")
        return self


class RuleConfig(RuleSpec):
    """One named YAML rule and its optional schedule/runtime gate."""

    name: str = Field(pattern=NAME_PATTERN)
    enabled: bool = True
    schedule: str | None = None

    @field_validator("schedule")
    @classmethod
    def _cron(cls, value: str | None) -> str | None:
        return validate_cron(value)

"""Pieces shared by the config models: names, sizes, cron and run batch limits."""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    BeforeValidator,
    ByteSize,
    ConfigDict,
    Field,
    model_validator,
)

from heliopuddle.durations import Duration

NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"


def _size_input(value: Any) -> Any:
    if isinstance(value, bool):
        raise ValueError("size must be bytes or a size string, not a boolean")
    return value


Size = Annotated[ByteSize, BeforeValidator(_size_input)]
"""A byte count accepting human-readable decimal and binary size strings."""


def validate_cron(value: str | None) -> str | None:
    if value is None:
        return None
    from prefect.client.schemas.schedules import CronSchedule

    try:
        CronSchedule(cron=value)
    except ValueError as e:
        raise ValueError(f"invalid cron expression {value!r}") from e
    return value


class BatchConfig(BaseModel):
    """Limits for one rule or index run, checked after each completed file."""

    model_config = ConfigDict(extra="forbid")

    max_files: int | None = Field(default=100, gt=0, strict=True)
    max_bytes: Size | None = Field(default=ByteSize(1_000_000_000), gt=0)
    max_duration: Duration | None = Field(default=timedelta(minutes=30), gt=timedelta())

    @model_validator(mode="after")
    def _bounded(self) -> BatchConfig:
        if self.max_files is None and self.max_bytes is None and self.max_duration is None:
            raise ValueError("at least one batch limit must be set")
        return self

"""Shared after-current-file limits for rule and index flow runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from time import monotonic

from pydantic import ByteSize

from heliopuddle.config.models import BatchConfig


@dataclass
class BatchBudget:
    """Track completed work against the first reached file, byte or duration limit."""

    limits: BatchConfig
    started: float = field(default_factory=lambda: monotonic())
    files: int = 0
    bytes: int = 0

    @property
    def exhausted(self) -> bool:
        """Whether to stop before starting another file; always allow the first file."""
        cfg = self.limits
        return self.files > 0 and (
            (cfg.max_files is not None and self.files >= cfg.max_files)
            or (cfg.max_bytes is not None and self.bytes >= cfg.max_bytes)
            or (
                cfg.max_duration is not None
                and monotonic() - self.started >= cfg.max_duration.total_seconds()
            )
        )

    def remaining(self) -> BatchConfig:
        """Return the remaining budget for a task that also stops after its current file."""
        cfg = self.limits
        return BatchConfig(
            max_files=max(1, cfg.max_files - self.files) if cfg.max_files is not None else None,
            max_bytes=(
                ByteSize(max(1, int(cfg.max_bytes) - self.bytes))
                if cfg.max_bytes is not None
                else None
            ),
            max_duration=(
                timedelta(
                    seconds=max(
                        0.000001, cfg.max_duration.total_seconds() - (monotonic() - self.started)
                    )
                )
                if cfg.max_duration is not None
                else None
            ),
        )

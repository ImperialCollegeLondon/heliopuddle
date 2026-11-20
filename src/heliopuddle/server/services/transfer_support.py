"""Result types and pure decisions of a verified transfer (SVC-19/20).

See docs/design/operations.md. Everything here is free of I/O except
:func:`_verified`'s read-back comparison, which only inspects an observation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from heliopuddle.config.models import RuleOptions
from heliopuddle.server.models.snapshots import CopySnapshot
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services.run_logging import run_logger
from heliopuddle.server.services.stores import Observation

logger = run_logger("heliopuddle.server.services.transfer")

Outcome = Literal[
    "copied",
    "moved",
    "deleted",
    "skipped",
    "conflict",
    "unchanged",
    "would_copy",
    "would_move",
    "would_delete",
]
"""``conflict`` newly records a differing destination as ``unknown``; ``unchanged`` found the
destination already available and identical, so nothing was written or notified; ``skipped``
leaves a newer or already-recorded conflicting destination as it is."""


@dataclass(frozen=True)
class TransferResult:
    """A completed per-file decision; failed operations raise instead of returning a result."""

    file_id: int
    path: str
    outcome: Outcome
    detail: str | None = None


class InsufficientSpace(OSError):
    """The destination cannot hold the source size plus the configured safety overhead."""


class TransferVerificationError(RuntimeError):
    """Read-back content does not match the captured source hash and size."""


def _matches(obs: Observation | None, source: CopySnapshot) -> bool:
    return obs is not None and (obs.hash, obs.size) == (source.file_hash, source.file_size)


def _decision(
    obs: Observation | None, source: CopySnapshot, options: RuleOptions
) -> Literal["same", "newer", "conflict", "write"]:
    if obs is None:
        return "write"
    if _matches(obs, source):
        return "same"
    if (
        options.update_only_if_newer
        and obs.mtime is not None
        and source.mtime is not None
        and obs.mtime > source.mtime
    ):
        return "newer"
    return "write" if options.overwrite else "conflict"


def _skip(source: CopySnapshot, decision: str, *, recorded: bool = True) -> TransferResult:
    detail = (
        "destination is newer than the source"
        if decision == "newer"
        else "destination differs and overwrite is disabled"
    )
    logger.info("transfer skipped %s: %s", source.path, detail)
    outcome: Outcome = "conflict" if decision == "conflict" and recorded else "skipped"
    return TransferResult(source.file_id, source.path, outcome, detail)


def _utc(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


def _recorded(copy: CopySnapshot | None, obs: Observation, *, available: bool) -> bool:
    """Whether the destination copy already records exactly this observation."""
    return (
        copy is not None
        and copy.token is None
        and copy.state == (FileState.AVAILABLE if available else FileState.UNKNOWN)
        and copy.hash == (obs.hash if available else None)
        and copy.size == obs.size
        and _utc(copy.mtime) == _utc(obs.mtime)
    )


def _verified(obs: Observation | None, source: CopySnapshot) -> Observation:
    if not _matches(obs, source) or obs is None:
        raise TransferVerificationError(
            f"transferred {source.path!r} failed read-back verification"
        )
    return obs


def _describe(result: TransferResult) -> str:
    detail = f" ({result.detail})" if result.detail else ""
    return f"{result.path!r}: {result.outcome}{detail}"


def _validate_request(source: str, destination: str, action: str) -> None:
    if action not in {"copy", "move"}:
        raise ValueError(f"invalid transfer action {action!r}")
    if source == destination:
        raise ValueError("source and destination storage locations must differ")

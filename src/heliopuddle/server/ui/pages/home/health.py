"""The health badge of a project card on the home page."""

from __future__ import annotations

from typing import Any

from heliopuddle.server.schemas import FileState


def project_health(summary: Any) -> tuple[str, str]:
    """A project card's badge text and tone from its summary."""
    missing = sum(sl.states.get(FileState.MISSING, 0) for sl in summary.storage_locations)
    if missing:
        return f"{missing:,} missing", "missing"
    if summary.views.needs_attention:
        return f"{summary.views.needs_attention:,} to review", "unknown"
    if summary.views.in_progress:
        return f"{summary.views.in_progress:,} in progress", "pending"
    return "healthy", "available"

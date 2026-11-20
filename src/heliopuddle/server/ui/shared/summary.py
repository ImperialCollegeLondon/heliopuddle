"""Project-summary helpers used by the home, Overview, Files and Storage pages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from heliopuddle.server.schemas import FileState
from heliopuddle.server.ui.shared.states import IN_PROGRESS


def view_counts(summary: Any) -> dict[str, int]:
    """Counts for the view tabs from a project summary (API-17)."""
    return {
        "all": summary.files,
        "latest": summary.latest_files,
        "attention": summary.views.needs_attention,
        "progress": summary.views.in_progress,
        "ignored": summary.views.ignored,
        "deleted": summary.deleted_files,
    }


@dataclass(frozen=True)
class Cell:
    """One heatmap cell or bar segment."""

    state: str
    label: str
    count: int
    share: float = 0.0
    url: str | None = None


@dataclass(frozen=True)
class AttentionItem:
    """One "needs attention" entry: a count of copies in one state on one storage location."""

    project: str
    storage_location: str
    state: str
    count: int
    url: str
    tone: str

    @property
    def title(self) -> str:
        """For example ``84 unknown copies on ingest``."""
        noun = "copy" if self.count == 1 else "copies"
        return f"{self.count:,} {self.state} {noun} on {self.storage_location}"


def attention_items(
    summary: Any, link: Any, *, include_progress: bool = False
) -> list[AttentionItem]:
    """Copies that need someone, per storage location: missing first, then unknown.

    Args:
        summary: a project summary (API-17).
        link: ``link(view, storage_location, state)`` gives the Files URL.
        include_progress: also list copies in progress (pending, overwriting, deleting).

    Returns:
        Entries with a non-zero count.
    """
    items: list[AttentionItem] = []
    groups: list[tuple[str, tuple[FileState, ...]]] = [
        ("attention", (FileState.MISSING, FileState.UNKNOWN))
    ]
    if include_progress:
        groups.append(("progress", IN_PROGRESS))
    for view, states in groups:
        for state in states:
            for sl in summary.storage_locations:
                count = sl.states.get(state, 0)
                if count:
                    items.append(
                        AttentionItem(
                            summary.project,
                            sl.name,
                            state.value,
                            count,
                            link(view, sl.name, state.value),
                            state.value,
                        )
                    )
    return items

"""The Files page's view tabs (UI-12)."""

from __future__ import annotations

from dataclasses import dataclass

from heliopuddle.server.schemas import FileState
from heliopuddle.server.ui.shared.states import ATTENTION, IN_PROGRESS


@dataclass(frozen=True)
class FileView:
    """A preset of the Files page (UI-12).

    Attributes:
        key: the ``view`` query value.
        label: the tab label.
        states: copy states the view selects (any of), or empty.
        tone: the count badge colour (a state key) or empty.
    """

    key: str
    label: str
    states: tuple[FileState, ...] = ()
    tone: str = ""


VIEWS: dict[str, FileView] = {
    v.key: v
    for v in (
        FileView("all", "All"),
        FileView("latest", "Latest"),
        FileView("attention", "Needs attention", ATTENTION, "missing"),
        FileView("progress", "In progress", IN_PROGRESS, "pending"),
        FileView("ignored", "Ignored by rules"),
        FileView("deleted", "Deleted"),
    )
}

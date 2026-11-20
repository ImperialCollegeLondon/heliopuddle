"""Copy states: which count as eligible, readable or in need of attention, and how each is drawn."""

from __future__ import annotations

from dataclasses import dataclass

from heliopuddle.server.schemas import FileState

ELIGIBLE = frozenset({FileState.PENDING, FileState.AVAILABLE})
READABLE = frozenset({FileState.AVAILABLE, FileState.OVERWRITING})
ATTENTION = (FileState.UNKNOWN, FileState.MISSING)
IN_PROGRESS = (FileState.PENDING, FileState.OVERWRITING, FileState.DELETING)


@dataclass(frozen=True)
class StateStyle:
    """How a copy state is drawn: a symbol and a CSS class (colour is never the only cue).

    Attributes:
        key: the state value, or ``none`` for "no copy".
        label: the text shown and read out.
        symbol: a one-character symbol.
    """

    key: str
    label: str
    symbol: str

    @property
    def css(self) -> str:
        """The CSS modifier class, e.g. ``st-available``."""
        return f"st-{self.key}"


STATE_STYLES: dict[str, StateStyle] = {
    "available": StateStyle("available", "available", "✓"),
    "pending": StateStyle("pending", "pending", "◌"),
    "overwriting": StateStyle("overwriting", "overwriting", "↻"),
    "unknown": StateStyle("unknown", "unknown", "?"),
    "missing": StateStyle("missing", "missing", "!"),
    "deleting": StateStyle("deleting", "deleting", "\u00d7"),
    "deleted": StateStyle("deleted", "deleted", "\u2013"),
    "none": StateStyle("none", "no copy", "\u2013"),
}
#: Copy states in the order the heatmap, bars and legends show them.
STATE_ORDER: tuple[FileState, ...] = (
    FileState.AVAILABLE,
    FileState.PENDING,
    FileState.OVERWRITING,
    FileState.UNKNOWN,
    FileState.MISSING,
    FileState.DELETING,
    FileState.DELETED,
)


def state_style(state: FileState | str | None) -> StateStyle:
    """The style of a copy state; None means "no copy"."""
    if state is None:
        return STATE_STYLES["none"]
    return STATE_STYLES[FileState(state).value]

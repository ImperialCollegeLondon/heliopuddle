"""A storage location's copy-state bar."""

from __future__ import annotations

from typing import Any

from heliopuddle.server.schemas import FileState, StorageLocationSummary
from heliopuddle.server.ui.shared.states import STATE_ORDER
from heliopuddle.server.ui.shared.summary import Cell


def bar_segments(sl: StorageLocationSummary, link: Any) -> list[Cell]:
    """A storage location's copy-state bar: live files split by the state of their copy here.

    Deleted copies are counted under "no copy", so the segments add up to the
    project's live files.

    Args:
        sl: the storage location's summary.
        link: ``link(storage_location, state)`` gives the Files URL for a segment.

    Returns:
        Segments with a share in percent, including zero-count ones.
    """
    cells = [
        (s.value, s.value, sl.states.get(s, 0), link(sl.name, s.value))
        for s in STATE_ORDER
        if s != FileState.DELETED
    ]
    cells.append(("none", "no copy", sl.no_copy, None))
    total = sum(c[2] for c in cells) or 1
    return [
        Cell(key, label, count, round(100 * count / total, 3), url if count else None)
        for key, label, count, url in cells
    ]

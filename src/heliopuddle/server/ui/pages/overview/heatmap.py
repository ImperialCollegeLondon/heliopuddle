"""The Overview's copy-state heatmap (UI-11)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from heliopuddle.server.schemas import FileState, StorageLocationSummary
from heliopuddle.server.ui.shared.states import STATE_ORDER, STATE_STYLES, StateStyle
from heliopuddle.server.ui.shared.summary import Cell


def heat_cells(
    sl: StorageLocationSummary, link: Any, states: Sequence[FileState] = STATE_ORDER
) -> list[Cell]:
    """The heatmap row of a storage location: one cell per state, then "no copy".

    Args:
        sl: the storage location's summary.
        link: ``link(storage_location, state)`` gives the Files URL for a cell.
        states: the state columns.

    Returns:
        Cells; non-zero state cells carry a URL ("no copy" is not filterable).
    """
    cells = [
        Cell(
            s.value,
            s.value,
            sl.states.get(s, 0),
            url=link(sl.name, s.value) if sl.states.get(s, 0) else None,
        )
        for s in states
    ]
    cells.append(Cell("none", "no copy", sl.no_copy))
    return cells


def heatmap(
    rows: Sequence[StorageLocationSummary], link: Any
) -> tuple[list[StateStyle], list[tuple[StorageLocationSummary, list[Cell]]]]:
    """The Overview heatmap: its state columns and a row of cells per storage location.

    A state, or "no copy", that is zero in every row gets no column (UI-11), so a
    healthy project shows a compact table.

    Args:
        rows: the storage locations' summaries, in config order.
        link: ``link(storage_location, state)`` gives the Files URL for a cell.

    Returns:
        The column styles (state order, "no copy" last) and, per storage location,
        its cells for exactly those columns.
    """
    keys = [s.value for s in STATE_ORDER] + ["none"]
    full = [(sl, heat_cells(sl, link)) for sl in rows]
    keep = [i for i in range(len(keys)) if any(cells[i].count for _sl, cells in full)]
    columns = [STATE_STYLES[keys[i]] for i in keep]
    return columns, [(sl, [cells[i] for i in keep]) for sl, cells in full]

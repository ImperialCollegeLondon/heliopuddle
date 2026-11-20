"""Paging and column choice on the Files page."""

from __future__ import annotations

from collections.abc import Sequence

from heliopuddle.server.schemas import StorageLocationSummary

PAGE_SIZES = (25, 50, 100, 200)
DEFAULT_PAGE_SIZE = 50
MAX_COLUMNS = 6
#: ``columns`` value meaning "no state columns" (never a storage location name).
NO_COLUMNS = "-"


def page_size(value: int | None) -> int:
    """Snap a requested page size to the allowed sizes (25, 50, 100 or 200)."""
    if value is None:
        return DEFAULT_PAGE_SIZE
    for size in PAGE_SIZES:
        if value <= size:
            return size
    return PAGE_SIZES[-1]


def choose_columns(
    storage_locations: Sequence[StorageLocationSummary], requested: str | None
) -> list[str]:
    """The storage locations shown as columns on the Files page (at most six).

    Args:
        storage_locations: the project's storage locations, in config order.
        requested: the ``columns`` query value (comma-separated names, or
            :data:`NO_COLUMNS`) or None.

    Returns:
        The requested known names in config order (none for :data:`NO_COLUMNS`),
        else the first six active ones (or the first six of all if none is
        active).
    """
    names = [s.name for s in storage_locations]
    if requested == NO_COLUMNS:
        return []
    if requested:
        wanted = {n.strip() for n in requested.split(",")}
        chosen = [n for n in names if n in wanted]
        if chosen:
            return chosen[:MAX_COLUMNS]
    active = [s.name for s in storage_locations if s.active] or names
    return active[:MAX_COLUMNS]

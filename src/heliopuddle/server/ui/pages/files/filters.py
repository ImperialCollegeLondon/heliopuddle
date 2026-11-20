"""The Files page's filters, parsed from the query string, and the chips that show them."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from heliopuddle.server.schemas import FileState
from heliopuddle.server.ui.pages.files.tabs import VIEWS
from heliopuddle.server.ui.shared.errors import BadRequestError
from heliopuddle.server.ui.shared.forms import TRUE_VALUES


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in TRUE_VALUES


def _parse_day(value: str, name: str) -> date | None:
    value = value.strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        raise BadRequestError(f"Invalid date for {name}: {value!r} (use YYYY-MM-DD).") from None


@dataclass(frozen=True)
class FileFilters:
    """Files page filters (UI-06, UI-12), parsed from GET query parameters.

    Attributes:
        view: the view key (see :data:`VIEWS`).
        path: path glob, or empty for any path.
        states: copy states (any of); empty means the view's states, if any.
        storage_location: only files with a copy here (in ``states`` if set).
        content_from: first content date, inclusive.
        content_to: last content date, inclusive.
        latest_only: only files that are the latest of their series.
        include_deleted: also list deleted files.
    """

    view: str = "all"
    path: str = ""
    states: tuple[FileState, ...] = ()
    storage_location: str = ""
    content_from: date | None = None
    content_to: date | None = None
    latest_only: bool = False
    include_deleted: bool = False

    @classmethod
    def parse(cls, params: Mapping[str, Sequence[str]]) -> FileFilters:
        """Build filters from query parameters (every value of each name).

        Checkboxes are on for ``1``, ``on``, ``true`` or ``yes`` (any case). ``state``
        may repeat or hold comma-separated values; empty values are ignored.

        Args:
            params: query parameter names mapped to their values.

        Returns:
            The parsed filters.

        Raises:
            BadRequestError: an unknown view or state, or a malformed date.
        """

        def first(name: str) -> str:
            values = params.get(name) or [""]
            return values[0]

        view = first("view").strip() or "all"
        if view not in VIEWS:
            raise BadRequestError(f"Unknown view {view!r}.")
        states: list[FileState] = []
        for raw in params.get("state", []):
            for part in raw.split(","):
                part = part.strip()
                if not part:
                    continue
                try:
                    parsed = FileState(part)
                except ValueError:
                    raise BadRequestError(f"Unknown state {part!r}.") from None
                if parsed not in states:
                    states.append(parsed)
        return cls(
            view=view,
            path=first("path").strip(),
            states=tuple(states),
            storage_location=first("storage_location").strip(),
            content_from=_parse_day(first("content_from"), "content_from"),
            content_to=_parse_day(first("content_to"), "content_to"),
            latest_only=_flag(first("latest_only")),
            include_deleted=_flag(first("include_deleted")),
        )

    @property
    def active(self) -> bool:
        """Whether any filter other than the view is set."""
        return replace(self, view="all") != FileFilters()

    @property
    def effective_states(self) -> tuple[FileState, ...]:
        """The states sent to the API: the chosen ones, else the view's."""
        return self.states or VIEWS[self.view].states

    def find_kwargs(self) -> dict[str, Any]:
        """Keyword arguments for :meth:`HelioPuddleClient.find`."""
        states = self.effective_states
        return {
            "path": self.path or None,
            "latest_only": self.latest_only or self.view == "latest",
            "state": list(states) if states else None,
            "storage_location": self.storage_location or None,
            "content_date_from": (
                datetime.combine(self.content_from, time.min, UTC) if self.content_from else None
            ),
            "content_date_to": (
                datetime.combine(self.content_to, time.min, UTC)
                + timedelta(days=1)
                - timedelta(microseconds=1)
                if self.content_to
                else None
            ),
            "include_deleted": self.include_deleted,
            "deleted_only": self.view == "deleted",
            "ignored_from_all_rules": True if self.view == "ignored" else None,
        }

    def query(self, *, view: bool = True) -> list[tuple[str, str]]:
        """The filters as query parameters, leaving out those not set."""
        params: list[tuple[str, str]] = []
        if view and self.view != "all":
            params.append(("view", self.view))
        if self.path:
            params.append(("path", self.path))
        params.extend(("state", s.value) for s in self.states)
        if self.storage_location:
            params.append(("storage_location", self.storage_location))
        if self.content_from:
            params.append(("content_from", self.content_from.isoformat()))
        if self.content_to:
            params.append(("content_to", self.content_to.isoformat()))
        if self.latest_only:
            params.append(("latest_only", "1"))
        if self.include_deleted:
            params.append(("include_deleted", "1"))
        return params


@dataclass(frozen=True)
class Chip:
    """An active filter shown as a removable chip."""

    label: str
    value: str
    remove_url: str


def filter_chips(filters: FileFilters, url: Any) -> list[Chip]:
    """The active filters as chips whose links remove them.

    Args:
        filters: the current filters.
        url: ``url(filters)`` returns the Files URL for other filters (paging reset).

    Returns:
        One chip per set filter (one per state).
    """
    chips: list[Chip] = []
    if filters.path:
        chips.append(Chip("Path", filters.path, url(replace(filters, path=""))))
    for state in filters.states:
        rest = tuple(s for s in filters.states if s != state)
        chips.append(Chip("State", state.value, url(replace(filters, states=rest))))
    if filters.storage_location:
        chips.append(
            Chip(
                "Storage location",
                filters.storage_location,
                url(replace(filters, storage_location="")),
            )
        )
    if filters.content_from or filters.content_to:
        start = filters.content_from.isoformat() if filters.content_from else "…"
        end = filters.content_to.isoformat() if filters.content_to else "…"
        chips.append(
            Chip(
                "Content date",
                f"{start} \u2013 {end}",
                url(replace(filters, content_from=None, content_to=None)),
            )
        )
    if filters.latest_only:
        chips.append(Chip("Latest only", "", url(replace(filters, latest_only=False))))
    if filters.include_deleted:
        chips.append(Chip("Include deleted", "", url(replace(filters, include_deleted=False))))
    return chips

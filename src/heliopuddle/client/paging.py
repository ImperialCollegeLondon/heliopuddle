"""Paged iteration shared by the real client and the in-memory fake (CLI-14)."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from datetime import datetime
from typing import Literal

from heliopuddle.server.schemas import FileList, FileRead, FileState

Order = Literal["-created_at", "path"]
"""Sort order of file listings: newest first, or by path."""


class FindAllMixin:
    """Provides :meth:`find_all` on top of a class's own :meth:`find`."""

    find: Callable[..., FileList]

    def find_all(
        self,
        path: str | None = None,
        latest_only: bool = False,
        state: FileState | str | Sequence[FileState | str] | None = None,
        storage_location: str | None = None,
        content_date_from: datetime | None = None,
        content_date_to: datetime | None = None,
        include_deleted: bool = False,
        series_id: int | None = None,
        deleted_only: bool = False,
        order: Order = "-created_at",
        page_size: int = 500,
        ignored_from_all_rules: bool | None = None,
    ) -> Iterator[FileRead]:
        """Iterate over every file matching the filters, fetching page by page.

        Takes the same filters as :meth:`find`. Pages of ``page_size`` (1 to
        1000) are fetched lazily; files added or removed while iterating may
        be skipped or seen twice.

        Yields:
            Each matching record, in ``order``.
        """
        offset = 0
        while True:
            page = self.find(
                path=path,
                latest_only=latest_only,
                state=state,
                storage_location=storage_location,
                content_date_from=content_date_from,
                content_date_to=content_date_to,
                include_deleted=include_deleted,
                series_id=series_id,
                deleted_only=deleted_only,
                limit=page_size,
                offset=offset,
                order=order,
                ignored_from_all_rules=ignored_from_all_rules,
            )
            yield from page.items
            offset += len(page.items)
            if not page.items or offset >= page.total:
                return

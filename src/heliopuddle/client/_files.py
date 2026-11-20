"""Reading and changing file records."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from heliopuddle.client._base import ClientBase
from heliopuddle.client.errors import NotFoundError
from heliopuddle.client.paging import FindAllMixin, Order
from heliopuddle.server.schemas import (
    AcceptRequest,
    ContentDateUpdate,
    FileList,
    FileRead,
    FileState,
    RulesIgnoreRequest,
    SeriesVersions,
)


class FilesMixin(FindAllMixin, ClientBase):
    """File lookups, listings, versions and record changes of :class:`HelioPuddleClient`."""

    def get_file(self, file_id: int) -> FileRead:
        """Return ``GET /files/{id}`` (live or deleted).

        Raises:
            NotFoundError: no such file.
        """
        return FileRead.model_validate(self._request("GET", f"/files/{file_id}").json())

    def get_file_by_path(self, path: str) -> FileRead:
        """Return the live file at ``path`` (``GET /projects/{p}/files/by-path``).

        Raises:
            NotFoundError: no live file at ``path``.
        """
        response = self._request(
            "GET", f"/projects/{self._project()}/files/by-path", params={"path": path}
        )
        return FileRead.model_validate(response.json())

    def list_files(self, limit: int = 100, offset: int = 0) -> FileList:
        """Return a page of ``GET /projects/{p}/files``, newest first."""
        return self.find(limit=limit, offset=offset)

    def find(
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
        limit: int = 100,
        offset: int = 0,
        order: Order = "-created_at",
        ignored_from_all_rules: bool | None = None,
    ) -> FileList:
        """Return one page of the project's files matching every given filter.

        Args:
            path: path glob: ``*`` matches within one segment, ``**`` any
                depth, ``?`` one character.
            latest_only: only the latest version of each series.
            state: only files with a copy in this state, or in any of a
                list of states (in ``storage_location`` if given).
            storage_location: only files with a (non-deleted) copy here.
            content_date_from: inclusive lower bound on the content date.
            content_date_to: inclusive upper bound on the content date.
            include_deleted: also return deleted files.
            deleted_only: only deleted files (implies ``include_deleted``).
            series_id: only files in this series.
            limit: page size, 1 to 1000.
            offset: number of matching files to skip.
            order: ``-created_at`` (newest first) or ``path``.
            ignored_from_all_rules: only ignored or non-ignored files; None means both.

        Returns:
            The page, with the ``total`` number of matching files.
        """
        params: dict[str, Any] = {
            "path": path,
            "series_id": series_id,
            "latest_only": latest_only,
            "state": state_values(state),
            "storage_location": storage_location,
            "content_date_from": iso(content_date_from),
            "content_date_to": iso(content_date_to),
            "include_deleted": include_deleted,
            "deleted_only": deleted_only or None,
            "limit": limit,
            "offset": offset,
            "order": order,
            "ignored_from_all_rules": ignored_from_all_rules,
        }
        response = self._request(
            "GET",
            f"/projects/{self._project()}/files",
            params={k: v for k, v in params.items() if v is not None},
        )
        return FileList.model_validate(response.json())

    def list_versions(
        self, path_or_series_id: str | int, include_deleted: bool = False
    ) -> SeriesVersions:
        """Return every version of a series, newest version first.

        Args:
            path_or_series_id: a series id, or the path of any file in the
                series (a deleted path also works).
            include_deleted: also return deleted versions.

        Raises:
            NotFoundError: unknown series, or no file ever existed at the path.
        """
        series_id = (
            path_or_series_id
            if isinstance(path_or_series_id, int)
            else self._series_id(path_or_series_id)
        )
        response = self._request(
            "GET", f"/series/{series_id}/versions", params={"include_deleted": include_deleted}
        )
        return SeriesVersions.model_validate(response.json())

    def latest(self, path: str) -> FileRead:
        """Return the latest live version in the series containing ``path``.

        ``path`` may be any version of the series, including a deleted one.

        Raises:
            NotFoundError: no file ever existed at ``path``, or the series has
                no live version.
        """
        page = self.find(series_id=self._series_id(path), latest_only=True, limit=1)
        if not page.items:
            raise NotFoundError(404, f"the series of {path!r} has no live version")
        return page.items[0]

    def _series_id(self, path: str) -> int:
        """Return the series of the live file at ``path``, else of a deleted one."""
        try:
            return self.get_file_by_path(path).series_id
        except NotFoundError:
            pass
        # ``path`` is a glob to the API; keep exact matches only.
        for file in self.find_all(path=path, include_deleted=True, page_size=100):
            if file.path == path:
                return file.series_id
        raise NotFoundError(404, f"no file at {path!r}")

    def _resolve(self, path_or_id: str | int) -> FileRead:
        """Return the record for a path, or for an id only if it is in this project (CLI-08).

        Raises:
            NotFoundError: no such file, or the id belongs to another project.
        """
        if isinstance(path_or_id, int):
            self._project()
            file = self.get_file(path_or_id)
            if file.project != self.project:
                raise NotFoundError(404, f"file {path_or_id} is not in project {self.project!r}")
            return file
        return self.get_file_by_path(path_or_id)

    # -- files: change -------------------------------------------------------

    def remove_file(self, path_or_id: str | int) -> FileRead:
        """Remove a live file from every storage location (``DELETE /files/{id}``).

        Args:
            path_or_id: the file id, or the path of a live file.

        Returns:
            The final record: ``deleted_at`` is set, or, if a store delete
            failed, the file is still live with a copy in state ``deleting``.

        Raises:
            NotFoundError: unknown or already deleted file, or no live file at the path.
        """
        file_id = self._resolve(path_or_id).id  # an id must be in this project (CLI-08)
        return FileRead.model_validate(self._request("DELETE", f"/files/{file_id}").json())

    def update_metadata(
        self, file_id: int, patch: dict[str, Any], replace: bool = False
    ) -> FileRead:
        """Change a live file's metadata (``PATCH /files/{id}/metadata``).

        Args:
            file_id: the file id.
            patch: keys to set; a ``None`` value removes the key.
            replace: replace the whole metadata with ``patch`` instead of merging.

        Raises:
            NotFoundError: unknown or deleted file, or a file of another project.
        """
        self._resolve(file_id)  # reject another project's file before changing it (CLI-08)
        response = self._request(
            "PATCH", f"/files/{file_id}/metadata", params={"replace": replace}, json=patch
        )
        return FileRead.model_validate(response.json())

    def set_content_date(self, path_or_id: str | int, content_date: datetime | None) -> FileRead:
        """Set a live file's content date (``PATCH /files/{id}/content-date``).

        Args:
            path_or_id: the file id, or the path of a live file.
            content_date: the new date (a naive time is UTC). None goes back to
                the date the project's version rules take from the path, or none.

        Returns:
            The updated record.

        Raises:
            NotFoundError: unknown or deleted file, no live file at the path,
                or an id from another project (nothing is changed).
        """
        file_id = self._resolve(path_or_id).id
        body = ContentDateUpdate(content_date=content_date)
        response = self._request(
            "PATCH", f"/files/{file_id}/content-date", json=body.model_dump(mode="json")
        )
        return FileRead.model_validate(response.json())

    def accept_file(self, path_or_id: str | int, storage_location: str | None = None) -> FileRead:
        """Accept a file's ``unknown`` copy as it is now (``POST /files/{id}/accept``).

        The server re-hashes the copy, takes the file's hash and size from it
        and sets the copy ``available``, which makes the file eligible for
        latest again.

        Args:
            path_or_id: the file id, or the path of a live file.
            storage_location: the storage location of the copy to accept. None
                means the file's only ``unknown`` copy.

        Returns:
            The updated record.

        Raises:
            NotFoundError: unknown or deleted file, no live file at the path,
                or an id from another project (nothing is accepted).
            HelioPuddleError: ``409`` if the copy is not ``unknown``; ``422`` if
                ``storage_location`` is None and the file has several
                ``unknown`` copies.
        """
        file_id = self._resolve(path_or_id).id
        body = AcceptRequest(storage_location=storage_location)
        response = self._request(
            "POST", f"/files/{file_id}/accept", json=body.model_dump(exclude_none=True)
        )
        return FileRead.model_validate(response.json())

    def set_rules_ignored(self, path_or_id: str | int, ignored: bool) -> FileRead:
        """Set a project's file exclusion from every rule, without affecting indexing."""
        file_id = self._resolve(path_or_id).id
        body = RulesIgnoreRequest(ignored=ignored)
        response = self._request("PATCH", f"/files/{file_id}/rules-ignore", json=body.model_dump())
        return FileRead.model_validate(response.json())


def state_values(state: FileState | str | Sequence[FileState | str] | None) -> list[str] | None:
    """Return a ``state`` filter as a list of query values (repeated params), or None."""
    if state is None:
        return None
    if isinstance(state, str):
        return [FileState(state).value]
    return [FileState(s).value for s in state] or None


def iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()

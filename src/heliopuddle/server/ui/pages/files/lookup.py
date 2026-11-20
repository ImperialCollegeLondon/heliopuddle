"""Fetching one file for the Files page, the inspector and the file page."""

from __future__ import annotations

from typing import Any

from heliopuddle.client import HelioPuddleClient, HelioPuddleError
from heliopuddle.server.schemas import FileRead, FileState


def copy_map(file: FileRead) -> dict[str, FileState]:
    """A file's copy states by storage-location name."""
    return {c.storage_location: c.state for c in file.storage_locations}


def project_file(client: HelioPuddleClient, project: str, file_id: int) -> FileRead | None:
    """The file, or None for a file that is not in this project."""
    try:
        file = client.get_file(file_id)
    except HelioPuddleError as exc:
        if exc.status_code == 404:
            return None
        raise
    return file if file.project == project else None


def inspector_context(
    client: HelioPuddleClient, project: str, file_id: int
) -> dict[str, Any] | None:
    """The inspector's data, or None for a file that is not in this project."""
    file = project_file(client, project, file_id)
    if file is None:
        return None
    versions = client.list_versions(file.series_id, include_deleted=True)
    return {"file": file, "versions": versions.items, "series_key": versions.series_key}

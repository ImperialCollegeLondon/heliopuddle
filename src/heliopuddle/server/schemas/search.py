"""Schemas for cross-project file search (API-16) and project summaries (API-17)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from heliopuddle.server.schemas.files import FileRead, FileState


class SearchResults(BaseModel):
    """A ranked page of files matching a search, across one or more projects."""

    items: list[FileRead]
    total: int = Field(description="Matches across every searched project")
    limit: int
    offset: int
    projects: dict[str, int] = Field(
        default_factory=dict,
        description="Matches per searched project; projects with none are left out",
    )


class StorageLocationSummary(BaseModel):
    """Copy counts for one storage location of a project."""

    name: str
    active: bool
    files: int = Field(description="Live files with a copy here that is not deleted")
    bytes: int = Field(description="Sum of those files' sizes")
    states: dict[FileState, int] = Field(
        description="Copies of live files in each state; every state is present"
    )
    no_copy: int = Field(description="Live files without a non-deleted copy here")


class ViewCounts(BaseModel):
    """Live-file counts for the Files page views."""

    needs_attention: int = Field(description="A copy is unknown or missing")
    in_progress: int = Field(description="A copy is pending, overwriting or deleting")
    ignored: int = Field(description="Ignored from all rules")


class ProjectSummary(BaseModel):
    """Aggregate figures for one project (API-17)."""

    project: str
    files: int = Field(description="Live files")
    latest_files: int
    bytes: int = Field(description="Sum of live file sizes")
    deleted_files: int
    storage_locations: list[StorageLocationSummary] = Field(description="In config order")
    views: ViewCounts

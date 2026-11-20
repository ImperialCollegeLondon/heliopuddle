"""Read a project's files: by id or path, filtered pages, and a series' versions."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import and_, exists, func, select
from sqlalchemy.orm import Session, joinedload, selectinload

from heliopuddle.globs import glob_match, literal_prefix
from heliopuddle.server.database.orm import (
    File,
    FileStorageLocation,
    Project,
    Series,
    StorageLocation,
)
from heliopuddle.server.models.files.errors import NotFoundError
from heliopuddle.server.models.series import sort_versions
from heliopuddle.server.schemas import FileState


def _file_options() -> tuple[Any, ...]:
    return (
        joinedload(File.project),
        joinedload(File.series),
        selectinload(File.copies).joinedload(FileStorageLocation.storage_location),
    )


def get_file(session: Session, file_id: int) -> File | None:
    """Return a file (live or deleted) with its project, series and copies loaded, or None."""
    return session.scalar(select(File).where(File.id == file_id).options(*_file_options()))


def project_name_of_file(session: Session, file_id: int) -> str | None:
    """Return the name of the project that owns a file (live or deleted), or None."""
    return session.scalar(
        select(Project.name).join(File, File.project_id == Project.id).where(File.id == file_id)
    )


def project_name_of_series(session: Session, series_id: int) -> str | None:
    """Return the name of the project that owns a series, or None."""
    return session.scalar(
        select(Project.name)
        .join(Series, Series.project_id == Project.id)
        .where(Series.id == series_id)
    )


def get_file_by_path(session: Session, project: str, path: str) -> File | None:
    """Return the live file at ``path`` in ``project`` (relations loaded), or None."""
    return session.scalar(
        select(File)
        .join(Project)
        .where(Project.name == project, File.path == path, File.deleted_at.is_(None))
        .options(*_file_options())
    )


Order = Literal["-created_at", "path"]

# Candidate rows fetched per batch when a path glob is matched in Python.
_GLOB_BATCH = 1000
_LIKE_ESCAPE = "\\"


def like_escape(text: str) -> str:
    """Escape ``\\``, ``%`` and ``_`` in ``text`` for a ``LIKE ... ESCAPE '\\'`` pattern."""
    return (
        text.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", _LIKE_ESCAPE + "%")
        .replace("_", _LIKE_ESCAPE + "_")
    )


def _states(state: FileState | Sequence[FileState] | None) -> list[FileState]:
    """Normalise a ``state`` filter to a list (``FileState`` is itself a ``str`` sequence)."""
    if state is None:
        return []
    if isinstance(state, str):
        return [FileState(state)]
    return [FileState(s) for s in state]


def list_files(
    session: Session,
    project: str,
    *,
    path: str | None = None,
    series_id: int | None = None,
    latest_only: bool = False,
    state: FileState | Sequence[FileState] | None = None,
    storage_location: str | None = None,
    content_date_from: datetime | None = None,
    content_date_to: datetime | None = None,
    include_deleted: bool = False,
    deleted_only: bool = False,
    limit: int = 100,
    offset: int = 0,
    order: Order = "-created_at",
    ignored_from_all_rules: bool | None = None,
) -> tuple[Sequence[File], int]:
    """Return a page of a project's files matching every given filter, and the total.

    A ``path`` glob (``*``, ``**``, ``?``) is narrowed in SQL with ``LIKE`` on
    its literal prefix, then matched exactly in Python before paging, so
    ``total`` counts only true matches. A path without wildcards is an exact
    match.

    Args:
        session: an open session.
        project: project name (an unknown project has no files).
        path: path glob matched against the whole relative path.
        series_id: only files of this series.
        latest_only: only files with ``is_latest``.
        state: only files with a copy in this state, or in any of these
            states (in ``storage_location`` when that is given too). An empty
            sequence is no filter.
        storage_location: only files with a copy in this storage location
            that is not ``deleted`` (in state ``state`` when that is given).
        content_date_from: only files with ``content_date >= content_date_from``.
        content_date_to: only files with ``content_date <= content_date_to``.
        include_deleted: also return deleted files.
        deleted_only: only deleted files (implies ``include_deleted``).
        limit: page size.
        offset: number of matching files to skip.
        order: ``-created_at`` (newest first) or ``path``; ties by id.
        ignored_from_all_rules: only ignored or non-ignored files; None means both.

    Returns:
        ``(files, total)``.
    """
    conditions: list[Any] = [Project.name == project]
    conditions += _row_conditions(
        series_id=series_id,
        latest_only=latest_only,
        content_date_from=content_date_from,
        content_date_to=content_date_to,
        include_deleted=include_deleted,
        deleted_only=deleted_only,
        ignored_from_all_rules=ignored_from_all_rules,
    )
    states = _states(state)
    if states or storage_location is not None:
        conditions.append(_copy_exists(states, storage_location))
    glob: str | None = None
    if path is not None:
        prefix = literal_prefix(path)
        if prefix == path:
            conditions.append(File.path == path)
        else:
            glob = path
            if prefix:
                conditions.append(File.path.like(like_escape(prefix) + "%", escape=_LIKE_ESCAPE))
    where = and_(*conditions)
    order_by = (
        (File.path.asc(), File.id.asc())
        if order == "path"
        else (File.created_at.desc(), File.id.desc())
    )

    if glob is None:
        total = session.scalar(select(func.count(File.id)).join(Project).where(where)) or 0
        files = session.scalars(
            select(File)
            .join(Project)
            .where(where)
            .order_by(*order_by)
            .limit(limit)
            .offset(offset)
            .options(*_file_options())
        ).all()
        return files, total
    return _glob_page(session, where, order_by, glob, limit=limit, offset=offset)


def _row_conditions(
    *,
    series_id: int | None,
    latest_only: bool,
    content_date_from: datetime | None,
    content_date_to: datetime | None,
    include_deleted: bool,
    deleted_only: bool,
    ignored_from_all_rules: bool | None,
) -> list[Any]:
    """The filters on the file row itself (not on its copies or path)."""
    conditions: list[Any] = []
    if deleted_only:
        conditions.append(File.deleted_at.is_not(None))
    elif not include_deleted:
        conditions.append(File.deleted_at.is_(None))
    if series_id is not None:
        conditions.append(File.series_id == series_id)
    if latest_only:
        conditions.append(File.is_latest.is_(True))
    if ignored_from_all_rules is not None:
        conditions.append(File.ignored_from_all_rules.is_(ignored_from_all_rules))
    if content_date_from is not None:
        conditions.append(File.content_date >= content_date_from)
    if content_date_to is not None:
        conditions.append(File.content_date <= content_date_to)
    return conditions


def _copy_exists(states: list[FileState], storage_location: str | None) -> Any:
    """An ``EXISTS`` filter on a file's copies (state and/or storage location)."""
    copies = select(FileStorageLocation.id).where(FileStorageLocation.file_id == File.id)
    if states:
        copies = copies.where(FileStorageLocation.state.in_(states))
    else:
        copies = copies.where(FileStorageLocation.state != FileState.DELETED)
    if storage_location is not None:
        copies = copies.join(StorageLocation).where(StorageLocation.name == storage_location)
    return exists(copies)


def _glob_page(
    session: Session,
    where: Any,
    order_by: tuple[Any, ...],
    glob: str,
    *,
    limit: int,
    offset: int,
) -> tuple[Sequence[File], int]:
    """Match ``glob`` in Python over the SQL-narrowed candidates and page the matches."""
    total = 0
    page_ids: list[int] = []
    candidates = select(File.id, File.path).join(Project).where(where).order_by(*order_by)
    rows = session.execute(candidates.execution_options(yield_per=_GLOB_BATCH))
    for file_id, file_path in rows:
        if not glob_match(file_path, glob):
            continue
        if offset <= total < offset + limit:
            page_ids.append(file_id)
        total += 1
    if not page_ids:
        return [], total
    by_id = {
        f.id: f
        for f in session.scalars(
            select(File).where(File.id.in_(page_ids)).options(*_file_options())
        )
    }
    return [by_id[i] for i in page_ids], total


def list_versions(
    session: Session, series_id: int, *, include_deleted: bool = False
) -> tuple[Series, str, list[File]]:
    """Return a series, its project name and its files, newest version first.

    Ties in version go to the newest ``created_at``, then the highest id.

    Args:
        session: an open session.
        series_id: the series.
        include_deleted: also return deleted files.

    Returns:
        ``(series, project_name, files)``.

    Raises:
        NotFoundError: if the series does not exist.
    """
    series = session.get(Series, series_id)
    if series is None:
        raise NotFoundError(f"series {series_id} not found")
    project_name = session.scalars(
        select(Project.name).where(Project.id == series.project_id)
    ).one()
    stmt = select(File).where(File.series_id == series_id).options(*_file_options())
    if not include_deleted:
        stmt = stmt.where(File.deleted_at.is_(None))
    files = sort_versions(session.scalars(stmt).all(), series.version_type)
    return series, project_name, files

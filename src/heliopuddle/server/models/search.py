"""Cross-project file search (API-16) and project summaries (API-17).

Both are read-only aggregate queries written to run on SQLite and Postgres
alike: conditional counts use ``sum(case ... end)`` rather than ``FILTER``.
Authorization belongs to the API layer; these functions take the names of the
projects the caller may read.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import and_, case, exists, func, literal, select
from sqlalchemy.orm import InstrumentedAttribute, Session
from sqlalchemy.sql.elements import ColumnElement

from heliopuddle.server.database.orm import File, FileStorageLocation, Project, StorageLocation
from heliopuddle.server.models.files import _LIKE_ESCAPE, _file_options, like_escape
from heliopuddle.server.schemas import (
    FileState,
    ProjectSummary,
    StorageLocationSummary,
    ViewCounts,
)

NEEDS_ATTENTION = (FileState.UNKNOWN, FileState.MISSING)
"""Copy states that put a file in the "needs attention" view."""
IN_PROGRESS = (FileState.PENDING, FileState.OVERWRITING, FileState.DELETING)
"""Copy states that put a file in the "in progress" view."""


@dataclass(frozen=True)
class SearchPage:
    """One page of search results.

    Attributes:
        files: the page, ranked (relations loaded for ``to_file_read``).
        total: matches across every searched project.
        per_project: matches per project; projects with none are left out.
    """

    files: Sequence[File]
    total: int
    per_project: dict[str, int] = field(default_factory=dict)


def search_words(q: str) -> list[str]:
    """Split a search string into lower-cased words on whitespace."""
    return [word.lower() for word in q.split()]


def _contains(
    column: ColumnElement[str] | InstrumentedAttribute[str], word: str
) -> ColumnElement[bool]:
    """``column LIKE '%word%'`` with ``word`` matched literally."""
    return column.like("%" + like_escape(word) + "%", escape=_LIKE_ESCAPE)


def search_files(
    session: Session,
    projects: Sequence[str],
    words: Sequence[str],
    *,
    latest_only: bool = False,
    include_deleted: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> SearchPage:
    """Return a ranked page of files whose path contains every word, and the counts.

    Each word must occur in the path case-insensitively as literal text (``%``,
    ``_``, ``*`` and ``?`` are not wildcards). The rank compares the first
    word with the file name: equal, then starts with, then contains, then a
    match only in the folder path; ties newest first, then by id descending.

    Args:
        session: an open session.
        projects: names of the projects to search (the caller may read them).
        words: lower-case words (see :func:`search_words`); must not be empty.
        latest_only: only files with ``is_latest``.
        include_deleted: also return deleted files.
        limit: page size.
        offset: number of ranked matches to skip.

    Returns:
        The page, the total and the matches per project.

    Raises:
        ValueError: if ``words`` is empty.
    """
    if not words:
        raise ValueError("a search needs at least one word")
    if not projects:
        return SearchPage(files=[], total=0)
    # Resolve the project ids first, so the scans below need no join.
    names: dict[int, str] = {
        project_id: name
        for project_id, name in session.execute(
            select(Project.id, Project.name).where(Project.name.in_(list(projects)))
        )
    }
    if not names:
        return SearchPage(files=[], total=0)
    path = File.path_lower
    conditions: list[Any] = [File.project_id.in_(list(names))]
    conditions += [_contains(path, word) for word in words]
    if not include_deleted:
        conditions.append(File.deleted_at.is_(None))
    if latest_only:
        conditions.append(File.is_latest.is_(True))
    where = and_(*conditions)

    counts = session.execute(
        select(File.project_id, func.count()).where(where).group_by(File.project_id)
    ).all()
    per_project = {names[project_id]: int(count) for project_id, count in counts if count}
    total = sum(per_project.values())
    if total == 0 or offset >= total:
        return SearchPage(files=[], total=total, per_project=per_project)

    name = File.name_lower
    first = words[0]
    rank = case(
        (name == literal(first), 0),
        (name.like(like_escape(first) + "%", escape=_LIKE_ESCAPE), 1),
        (_contains(name, first), 2),
        else_=3,
    )
    # Rank and page narrow rows (ids only), then load the page's files.
    page_ids = list(
        session.scalars(
            select(File.id)
            .where(where)
            .order_by(rank, File.created_at.desc(), File.id.desc())
            .limit(limit)
            .offset(offset)
        )
    )
    by_id = {
        file.id: file
        for file in session.scalars(
            select(File).where(File.id.in_(page_ids)).options(*_file_options())
        )
    }
    return SearchPage(files=[by_id[i] for i in page_ids], total=total, per_project=per_project)


def _count(condition: ColumnElement[bool]) -> ColumnElement[int]:
    """``sum(case when condition then 1 else 0 end)``: a portable ``count(*) FILTER``."""
    return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)


def _copy_in(states: Sequence[FileState]) -> ColumnElement[bool]:
    return exists(
        select(FileStorageLocation.id).where(
            FileStorageLocation.file_id == File.id, FileStorageLocation.state.in_(states)
        )
    )


def project_summary(session: Session, project: str, configured: Sequence[str]) -> ProjectSummary:
    """Return aggregate figures for one project (API-17).

    Storage locations come in ``configured`` order (all active), then any
    inactive storage location the database still holds, oldest first. A
    configured storage location without a database row yet has zeros.

    Args:
        session: an open session.
        project: project name (an unknown project has all zeros).
        configured: the project's storage location names, in config order.

    Returns:
        The summary.
    """
    project_id = session.scalar(select(Project.id).where(Project.name == project))
    zero_states: dict[FileState, int] = dict.fromkeys(FileState, 0)
    if project_id is None:
        return ProjectSummary(
            project=project,
            files=0,
            latest_files=0,
            bytes=0,
            deleted_files=0,
            storage_locations=[
                StorageLocationSummary(
                    name=name, active=True, files=0, bytes=0, states=dict(zero_states), no_copy=0
                )
                for name in configured
            ],
            views=ViewCounts(needs_attention=0, in_progress=0, ignored=0),
        )
    live = File.deleted_at.is_(None)
    in_project = File.project_id == project_id

    # 1. live and deleted files.
    files, latest, size, deleted = session.execute(
        select(
            _count(live),
            _count(and_(live, File.is_latest.is_(True))),
            func.coalesce(func.sum(case((live, File.size), else_=0)), 0),
            _count(File.deleted_at.is_not(None)),
        ).where(in_project)
    ).one()

    # 2. copies of live files, by storage location and state, and per storage
    # location the live files (and their bytes) with a copy that is not deleted.
    states: dict[int, dict[FileState, int]] = {}
    for location_id, state, count in session.execute(
        select(FileStorageLocation.storage_location_id, FileStorageLocation.state, func.count())
        .join(File, File.id == FileStorageLocation.file_id)
        .where(in_project, live)
        .group_by(FileStorageLocation.storage_location_id, FileStorageLocation.state)
    ):
        states.setdefault(location_id, dict(zero_states))[FileState(state)] = int(count)
    held: dict[int, tuple[int, int]] = {
        location_id: (int(count), int(total or 0))
        for location_id, count, total in session.execute(
            select(FileStorageLocation.storage_location_id, func.count(), func.sum(File.size))
            .join(File, File.id == FileStorageLocation.file_id)
            .where(in_project, live, FileStorageLocation.state != FileState.DELETED)
            .group_by(FileStorageLocation.storage_location_id)
        )
    }

    # 3. the Files page views.
    attention, progress, ignored = session.execute(
        select(
            _count(_copy_in(NEEDS_ATTENTION)),
            _count(_copy_in(IN_PROGRESS)),
            _count(File.ignored_from_all_rules.is_(True)),
        ).where(in_project, live)
    ).one()

    rows = {
        row.name: row
        for row in session.scalars(
            select(StorageLocation)
            .where(StorageLocation.project_id == project_id)
            .order_by(StorageLocation.id)
        )
    }
    names = list(configured) + [n for n, r in rows.items() if n not in configured and not r.active]
    locations: list[StorageLocationSummary] = []
    for name in names:
        row = rows.get(name)
        count, total = held.get(row.id, (0, 0)) if row else (0, 0)
        locations.append(
            StorageLocationSummary(
                name=name,
                active=row.active if row else True,
                files=count,
                bytes=total,
                states=states.get(row.id, dict(zero_states)) if row else dict(zero_states),
                no_copy=int(files) - count,
            )
        )
    return ProjectSummary(
        project=project,
        files=int(files),
        latest_files=int(latest),
        bytes=int(size),
        deleted_files=int(deleted),
        storage_locations=locations,
        views=ViewCounts(
            needs_attention=int(attention), in_progress=int(progress), ignored=int(ignored)
        ),
    )

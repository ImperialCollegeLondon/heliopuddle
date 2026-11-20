"""SQL-prefiltered rule selection and stable change-key pagination (SVC-04/05/22)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, Row, Select, and_, case, func, or_, select
from sqlalchemy.orm import InstrumentedAttribute, Session

from heliopuddle.config.models import AgeField, RuleMatch, age_cutoff
from heliopuddle.globs import glob_match, literal_prefix
from heliopuddle.server.database.orm import (
    File,
    FileStorageLocation,
    Project,
    RuleState,
    StorageLocation,
)
from heliopuddle.server.models.files import like_escape
from heliopuddle.server.schemas import FileState
from heliopuddle.server.schemas.rules import RuleCursor


@dataclass(frozen=True)
class RuleCandidate:
    """A selected source snapshot and its file/copy change key."""

    file_id: int
    path: str
    size: int
    generation: int
    hash: str
    cursor: RuleCursor


def change_time() -> ColumnElement[datetime]:
    """Return the later file/source-copy update time, portable to SQLite and Postgres."""
    return case(
        (FileStorageLocation.updated_at > File.updated_at, FileStorageLocation.updated_at),
        else_=File.updated_at,
    )


def after_key(at: ColumnElement[datetime], cursor: RuleCursor) -> ColumnElement[bool]:
    """Compare both components of a file change key, never dropping timestamp ties."""
    return or_(at > cursor.updated_at, and_(at == cursor.updated_at, File.id > cursor.id))


def _column_after(
    column: InstrumentedAttribute[datetime], cursor: RuleCursor
) -> ColumnElement[bool]:
    """``(column, file id) > cursor``, written so an ``(updated_at, id)`` index can seek."""
    return and_(
        column >= cursor.updated_at,
        or_(column > cursor.updated_at, File.id > cursor.id),
    )


def through_time(through: datetime) -> ColumnElement[bool]:
    """``change_time() <= through``, written so each timestamp index can bound it."""
    return and_(File.updated_at <= through, FileStorageLocation.updated_at <= through)


def change_key_page(
    session: Session,
    base: Select[*tuple[Any, ...]],
    *,
    lower: RuleCursor | None,
    size: int,
    extra: Sequence[Select[*tuple[Any, ...]]] = (),
) -> tuple[list[Row[*tuple[Any, ...]]], bool]:
    """Return the next rows by change key ``(change_time(), file id)``, using the indexes.

    The change key is the later of the file's and the source copy's
    ``updated_at``, which no index can order. So the page is read as two
    index-ordered streams, rows with ``(file.updated_at, id) > lower`` and rows
    with ``(copy.updated_at, id) > lower``, each limited to ``size``, then
    merged. A row neither stream returned has a change key above the last key
    read from a full stream (a row's change key is at least each of its two
    timestamps), so only rows at or below the smallest such key are returned:
    nothing is skipped and no row is returned out of order.

    Args:
        session: an open session.
        base: the selection without any change-key condition; its first column
            is ``File.id`` and it has a ``changed_at`` column (``change_time()``).
            It must select at most one row per file.
        lower: return only rows whose change key is after this cursor.
        size: rows read per stream.
        extra: further selections already ordered by the change key, with
            their own cursor conditions (rows that qualify for another reason,
            e.g. an age crossing), merged the same way.

    Returns:
        The rows in change-key order, and whether every stream was exhausted
        (no rows remain after the last one returned). An empty, unexhausted
        page means a larger ``size`` is needed.
    """
    streams = [
        base.where(_column_after(File.updated_at, lower) if lower else File.id > 0)
        .order_by(File.updated_at, File.id)
        .limit(size),
        base.where(_column_after(FileStorageLocation.updated_at, lower) if lower else File.id > 0)
        .order_by(FileStorageLocation.updated_at, File.id)
        .limit(size),
    ]
    keys: list[ColumnElement[datetime] | InstrumentedAttribute[datetime]] = [
        File.updated_at,
        FileStorageLocation.updated_at,
    ]
    streams += [query.limit(size) for query in extra]
    keys += [change_time()] * len(extra)
    rows: dict[int, Row[*tuple[Any, ...]]] = {}
    bound: tuple[datetime, int] | None = None
    for query, key in zip(streams, keys, strict=True):
        page = session.execute(query.add_columns(key.label("stream_key"))).all()
        for row in page:
            rows.setdefault(row[0], row)
        if len(page) == size:
            last = page[-1]
            limit = (last.stream_key, last[0])
            bound = limit if bound is None or limit < bound else bound
    ordered = sorted(rows.values(), key=lambda row: (row.changed_at, row[0]))
    if bound is not None:
        ordered = [row for row in ordered if (row.changed_at, row[0]) <= bound]
    return ordered, bound is None


AgeColumn = (
    ColumnElement[datetime]
    | InstrumentedAttribute[datetime]
    | InstrumentedAttribute[datetime | None]
)


def age_column(field: AgeField) -> AgeColumn:
    """Return the date an age criterion compares (docs/design/rules.md, "Age criteria").

    ``modified`` is the source copy's mtime, else when hp first recorded the
    file; ``created`` when hp first recorded it; ``content_date`` the file's
    content date (NULL never matches); ``arrived`` the source copy's arrival.
    """
    if field == "modified":
        return func.coalesce(FileStorageLocation.mtime, File.created_at)
    if field == "created":
        return File.created_at
    if field == "content_date":
        return File.content_date
    return FileStorageLocation.arrived_at


def age_conditions(match: RuleMatch, now: datetime) -> list[ColumnElement[bool]]:
    """Every age bound as a SQL condition at selection time ``now``.

    ``older_than`` selects dates strictly before its cutoff, ``younger_than``
    dates at or after it.
    """
    conditions: list[ColumnElement[bool]] = []
    for field, bounds in match.age.items():
        column = age_column(field)
        if bounds.older_than is not None:
            conditions.append(column < age_cutoff(bounds.older_than, now))
        if bounds.younger_than is not None:
            conditions.append(column >= age_cutoff(bounds.younger_than, now))
    return conditions


def age_crossings(match: RuleMatch, since: datetime) -> ColumnElement[bool] | None:
    """Files that crossed a relative ``older_than`` bound after selection time ``since``.

    A relative ``older_than: d`` holds at ``now`` when ``date < now - d``; it
    did not yet hold at ``since`` when ``date >= since - d``. Only relative
    ``older_than`` bounds make files newly eligible as time passes: absolute
    bounds never move, and ``younger_than`` only ever excludes more files.
    The selection's own conditions keep ``date < now - d``.

    Returns:
        An OR over every such bound, or None when the match has none.
    """
    crossings = [
        age_column(field) >= since - bounds.older_than
        for field, bounds in match.age.items()
        if isinstance(bounds.older_than, timedelta)
    ]
    return or_(*crossings) if crossings else None


def select_candidates(
    session: Session,
    project_id: int,
    source_id: int,
    match: RuleMatch,
    *,
    now: datetime,
    after: RuleCursor | None = None,
    changed_after: RuleCursor | None = None,
    age_since: datetime | None = None,
    through: datetime | None = None,
    file_ids: Sequence[int] | None = None,
    limit: int = 1000,
    after_id: int | None = None,
) -> list[RuleCandidate]:
    """Select at most ``limit`` exact matches, using bounded SQL keyset pages.

    SQL excludes inactive sources, owned/unavailable copies, deleted/ignored
    files, every age bound and escaped literal glob prefixes. Exact include and
    exclude matching follows; rejected candidates never consume the limit.

    ``changed_after`` restricts an incremental selection to changed files or
    source copies, plus files that crossed a relative ``older_than`` bound
    since ``age_since`` (:func:`age_crossings`). ``after`` advances
    within that selection without changing its age window. ``through`` can
    bound a snapshot's change time. Full runs instead pass ``after_id`` to
    walk immutable file ids: their own writes cannot move work behind the
    cursor or make a file eligible twice.

    ``file_ids`` restricts the same matcher for best-effort change triggers.
    No state is written by selection.
    """
    if limit <= 0 or file_ids == []:
        return []
    updated = change_time()
    base = _base_selection(
        project_id, source_id, match, now=now, updated=updated, through=through, file_ids=file_ids
    )
    changed: ColumnElement[bool] | None = None
    if changed_after is not None:
        changed = after_key(updated, changed_after)
    crossed: ColumnElement[bool] | None = None
    if changed_after is not None and age_since is not None:
        crossed = age_crossings(match, age_since)
    selected: list[RuleCandidate] = []
    page_size = min(max(limit, 100), 1000)
    if after_id is not None:
        # Full runs walk immutable file ids, so the primary key orders them.
        if changed is not None:
            base = base.where(or_(changed, crossed) if crossed is not None else changed)
        _scan_by_id(session, base, match, selected, limit, after_id, page_size)
        return selected
    _scan_by_change_key(
        session,
        base,
        match,
        selected,
        limit,
        page_size,
        updated=updated,
        after=after,
        changed_after=changed_after,
        crossed=crossed,
    )
    return selected


def _base_selection(
    project_id: int,
    source_id: int,
    match: RuleMatch,
    *,
    now: datetime,
    updated: ColumnElement[datetime],
    through: datetime | None,
    file_ids: Sequence[int] | None,
) -> Select[*tuple[Any, ...]]:
    """The SQL prefilter of :func:`select_candidates`, before any change-key condition."""
    conditions = [
        File.project_id == project_id,
        File.deleted_at.is_(None),
        File.ignored_from_all_rules.is_(False),
        StorageLocation.project_id == project_id,
        StorageLocation.id == source_id,
        StorageLocation.active.is_(True),
        FileStorageLocation.state == FileState.AVAILABLE,
        FileStorageLocation.op_token.is_(None),
        or_(
            *(
                File.path.like(like_escape(literal_prefix(pattern)) + "%", escape="\\")
                for pattern in match.paths
            )
        ),
    ]
    if match.is_latest is not None:
        conditions.append(File.is_latest.is_(match.is_latest))
    conditions += age_conditions(match, now)
    if file_ids is not None:
        conditions.append(File.id.in_(file_ids))
    columns = (
        File.id,
        File.path,
        func.coalesce(FileStorageLocation.size, File.size),
        FileStorageLocation.generation,
        File.hash,
        updated.label("changed_at"),
    )
    base = (
        select(*columns)
        .join(FileStorageLocation, FileStorageLocation.file_id == File.id)
        .join(StorageLocation, StorageLocation.id == FileStorageLocation.storage_location_id)
        .where(*conditions)
    )
    if through is not None:
        base = base.where(through_time(through))
    return base


def _take(
    rows: Sequence[Row[*tuple[Any, ...]]],
    match: RuleMatch,
    selected: list[RuleCandidate],
    limit: int,
) -> RuleCursor | None:
    """Append the rows that match exactly (include and exclude globs); return the last cursor."""
    cursor = None
    for row in rows:
        file_id, path, size, generation, digest, at = row[:6]
        cursor = RuleCursor(updated_at=at, id=file_id)
        if not any(glob_match(path, p) for p in match.paths) or any(
            glob_match(path, p) for p in match.exclude
        ):
            continue
        selected.append(RuleCandidate(file_id, path, size, generation, digest, cursor))
        if len(selected) == limit:
            break
    return cursor


def _scan_by_id(
    session: Session,
    base: Select[*tuple[Any, ...]],
    match: RuleMatch,
    selected: list[RuleCandidate],
    limit: int,
    after_id: int,
    page_size: int,
) -> None:
    """Fill ``selected`` by walking file ids after ``after_id`` in keyset pages."""
    while len(selected) < limit:
        rows = session.execute(
            base.where(File.id > after_id).order_by(File.id).limit(page_size)
        ).all()
        if not rows:
            break
        _take(rows, match, selected, limit)
        after_id = rows[-1][0]
        if len(rows) < page_size:
            break


def _scan_by_change_key(
    session: Session,
    base: Select[*tuple[Any, ...]],
    match: RuleMatch,
    selected: list[RuleCandidate],
    limit: int,
    page_size: int,
    *,
    updated: ColumnElement[datetime],
    after: RuleCursor | None,
    changed_after: RuleCursor | None,
    crossed: ColumnElement[bool] | None,
) -> None:
    """Fill ``selected`` by paging the change key (:func:`change_key_page`) after ``after``."""
    while len(selected) < limit:
        # Changed rows come from the two index-ordered streams; rows that only
        # crossed an age threshold are a third stream (see change_key_page).
        lower = after
        if changed_after is not None and (
            lower is None
            or (lower.updated_at, lower.id) < (changed_after.updated_at, changed_after.id)
        ):
            lower = changed_after
        extra = []
        if crossed is not None:
            query = base.where(crossed)
            if after is not None:
                query = query.where(after_key(updated, after))
            extra.append(query.order_by(updated, File.id))
        rows, exhausted = change_key_page(session, base, lower=lower, size=page_size, extra=extra)
        if not rows:
            if exhausted:
                break
            page_size *= 2
            continue
        after = _take(rows, match, selected, limit) or after
        if exhausted:
            break


def effective_enabled(session: Session, project_id: int, name: str, yaml_enabled: bool) -> bool:
    """Read both enablement gates without creating or resetting runtime state."""
    state = session.get(RuleState, (project_id, name))
    return yaml_enabled and (state is None or state.enabled)


def record_progress(
    session: Session,
    project_id: int,
    name: str,
    cursor: RuleCursor | None,
    *,
    completed_at: datetime | None = None,
) -> None:
    """Save completed work without ever overwriting the runtime enable switch."""
    session.get_one(Project, project_id, with_for_update=True)
    state = session.get(RuleState, (project_id, name))
    if state is None:
        state = RuleState(project_id=project_id, rule_name=name)
        session.add(state)
    if cursor is not None and (
        state.watermark_at is None
        or (cursor.updated_at, cursor.id) > (state.watermark_at, state.watermark_id or 0)
    ):
        state.watermark_at, state.watermark_id = cursor.updated_at, cursor.id
    if completed_at is not None:
        state.last_success_at = completed_at


def is_live_unignored(session: Session, file_id: int) -> bool:
    """Whether a file still exists and is not ignored from all rules.

    Used to re-check a selected candidate just before it is executed, since
    the file may have been deleted or flagged after its page was read.
    """
    return (
        session.scalar(
            select(File.id).where(
                File.id == file_id,
                File.deleted_at.is_(None),
                File.ignored_from_all_rules.is_(False),
            )
        )
        is not None
    )

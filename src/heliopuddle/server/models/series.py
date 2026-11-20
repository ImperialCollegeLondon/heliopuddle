"""Series: upsert, row lock and ``is_latest`` maintenance (API-02, DB-02).

See docs/design/versioning.md ("Ordering") and docs/design/data-model.md.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import ColumnElement, and_, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from heliopuddle.server.database.orm import File, FileStorageLocation, Series, utcnow
from heliopuddle.server.schemas import FileState
from heliopuddle.versioning import VersionInfo, display_series_key


class VersionKindMismatch(ValueError):
    """A version of the other kind than its series holds (API-21, ``422``).

    Attributes:
        series_key: the stored series key.
        series_type: the series' kind (``number`` or ``text``).
        version_type: the kind of the rejected version.
    """

    def __init__(self, series_key: str, series_type: str, version_type: str) -> None:
        super().__init__(
            f"series {display_series_key(series_key)!r} holds {series_type} versions; "
            f"a {version_type} version cannot be added to it"
        )
        self.series_key = series_key
        self.series_type = series_type
        self.version_type = version_type


def lock_series(session: Session, project_id: int, info: VersionInfo) -> Series:
    """Return the series for ``info.series_key``, creating it if needed, row-locked.

    The row is selected with ``FOR UPDATE`` (Postgres; on SQLite the
    ``BEGIN IMMEDIATE`` write transaction already serialises writers). A
    concurrent insert of the same key is resolved with a savepoint: the loser
    selects and locks the winner's row. The series' kind is not touched: a
    new file claims it with :func:`claim_version_type`.

    Args:
        session: a session inside a write transaction.
        project_id: the project.
        info: the derived series and version of the path being written.

    Returns:
        The locked series row.
    """
    stmt = (
        select(Series)
        .where(Series.project_id == project_id, Series.series_key == info.series_key)
        .with_for_update()
    )
    series = session.scalar(stmt)
    if series is None:
        try:
            with session.begin_nested():
                series = Series(project_id=project_id, series_key=info.series_key)
                session.add(series)
        except IntegrityError:
            series = session.scalars(stmt).one()
    return series


def claim_version_type(series: Series, info: VersionInfo) -> None:
    """Check a new file's version kind against its locked series (API-21).

    The first versioned file sets ``series.version_type``; it never changes
    after that. Unversioned files fit any series.

    Args:
        series: the series, locked by :func:`lock_series`.
        info: the new file's series and version (after any override).

    Raises:
        VersionKindMismatch: the series holds versions of the other kind.
    """
    kind = info.version_type
    if kind is None:
        return
    if series.version_type is None:
        series.version_type = kind
    elif series.version_type != kind:
        raise VersionKindMismatch(series.series_key, series.version_type, kind)


def lock_series_by_id(session: Session, series_id: int) -> Series:
    """Return the series row ``series_id``, locked with ``FOR UPDATE``."""
    return session.scalars(select(Series).where(Series.id == series_id).with_for_update()).one()


VersionKey = tuple[int, int] | tuple[int, str]


def version_key(file: File, version_type: str | None) -> VersionKey:
    """Ordering key of ``file`` within its series; the highest key is the latest.

    Only the column of the series' kind counts: ``version_number`` for a
    ``number`` series, ``version_text`` (compared as bytes) for a ``text``
    series. Unversioned files (and a series with no kind) rank lowest.
    """
    if version_type == "number" and file.version_number is not None:
        return (1, file.version_number)
    if version_type == "text" and file.version_text is not None:
        return (1, file.version_text)
    return (0, 0)


def ordering_key(file: File, version_type: str | None) -> tuple[VersionKey, float, int]:
    """Full ordering key: version, then newest ``created_at``, then highest id.

    Text compares by code point, which is UTF-8 byte order, as in the database.
    """
    return (version_key(file, version_type), file.created_at.timestamp(), file.id)


def sort_versions(files: Sequence[File], version_type: str | None) -> list[File]:
    """Return ``files`` newest version first (ties: newest, then highest id)."""
    return sorted(files, key=lambda f: ordering_key(f, version_type), reverse=True)


LATEST_ELIGIBLE_STATES = frozenset({FileState.AVAILABLE, FileState.OVERWRITING})
"""States that make a live file latest-eligible, plus ``pending`` without a token (API-12)."""


def latest_eligible_copy() -> ColumnElement[bool]:
    """SQL condition on ``file_storage_location``: the copy makes its live file eligible.

    ``available`` or ``overwriting``, or ``pending`` without an operation token
    (see docs/design/data-model.md, ``is_latest``).
    """
    return or_(
        FileStorageLocation.state.in_(sorted(LATEST_ELIGIBLE_STATES)),
        and_(
            FileStorageLocation.state == FileState.PENDING, FileStorageLocation.op_token.is_(None)
        ),
    )


def _version_order(version_type: str | None) -> list[ColumnElement[Any]]:
    """``ORDER BY`` terms (descending) matching :func:`ordering_key`.

    Only the column of the series' kind is sorted, so the per-series index on
    it can serve the query. ``COALESCE`` puts unversioned rows last without
    ``NULLS LAST``, which SQLite and Postgres order differently. Text sorts
    byte-wise (``COLLATE "C"`` on Postgres, binary on SQLite).
    """
    terms: list[ColumnElement[Any]] = []
    if version_type == "number":
        terms.append(func.coalesce(File.version_number, -1))
    elif version_type == "text":
        terms.append(func.coalesce(File.version_text, ""))
    terms += [File.created_at, File.id]
    return [t.desc() for t in terms]


def recompute_latest(session: Session, series: Series) -> int | None:
    """Recompute ``is_latest`` for the files of ``series`` and ``series.latest_file_id``.

    The caller must hold the series row lock (:func:`lock_series`) in the same
    write transaction. Only **eligible** files are considered: live
    (non-deleted) files with at least one copy that is ``available``,
    ``overwriting`` or ``pending`` without a token (API-12). Among them exactly
    one becomes latest: the highest version of the series' kind; ties
    go to the newest ``created_at``, then the highest id. If no file is
    eligible, none is latest and ``latest_file_id`` is NULL.

    The top file is selected in SQL (``ORDER BY ... LIMIT 1``) and the flags
    are set with two bulk ``UPDATE`` statements, so no member of the series is
    loaded (SVC-17). Files whose flags flip get a new ``updated_at`` so
    incremental rules observe the change (SVC-05).

    Args:
        session: a session inside a write transaction.
        series: the locked series.

    Returns:
        The latest file's id, or None if the series has no eligible file.
    """
    session.flush()
    eligible = exists(
        select(FileStorageLocation.id).where(
            FileStorageLocation.file_id == File.id, latest_eligible_copy()
        )
    )
    latest_id = session.scalar(
        select(File.id)
        .where(File.series_id == series.id, File.deleted_at.is_(None), eligible)
        .order_by(*_version_order(series.version_type))
        .limit(1)
    )
    changed_at = utcnow()
    clear = (
        update(File)
        .where(File.series_id == series.id, File.is_latest.is_(True))
        .values(is_latest=False, updated_at=changed_at)
    )
    if latest_id is not None:
        clear = clear.where(File.id != latest_id)
    session.execute(clear, execution_options={"synchronize_session": False})
    if latest_id is not None:
        session.execute(
            update(File)
            .where(File.id == latest_id, File.is_latest.is_(False))
            .values(is_latest=True, updated_at=changed_at),
            execution_options={"synchronize_session": False},
        )
    # Keep loaded File objects in step without expiring their other attributes.
    for obj in list(session.identity_map.values()):
        if isinstance(obj, File) and obj.series_id == series.id:
            if obj.is_latest != (obj.id == latest_id):
                set_committed_value(obj, "updated_at", changed_at)
            set_committed_value(obj, "is_latest", obj.id == latest_id)
    series.latest_file_id = latest_id
    session.flush()
    return latest_id


def lock_series_of_file(session: Session, file_id: int) -> Series | None:
    """Return the series row of ``file_id`` locked with ``FOR UPDATE``, or None if no such file."""
    series_id = session.scalar(select(File.series_id).where(File.id == file_id))
    if series_id is None:
        return None
    return lock_series_by_id(session, series_id)

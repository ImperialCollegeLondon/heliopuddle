"""Insert file and copy rows directly, for golden datasets of query tests.

Going around the API keeps the setup fast and lets a test put copies in any
state, which the API only reaches through long sequences of operations.
"""

from __future__ import annotations

import posixpath
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from heliopuddle.server.database.orm import (
    File,
    FileStorageLocation,
    Project,
    Series,
    StorageLocation,
)
from heliopuddle.server.schemas import FileState

T0 = datetime(2026, 1, 1, tzinfo=UTC)
"""Base creation time; ``add_file(..., age=n)`` is ``n`` minutes later."""


def storage_location_id(session: Session, project: str, name: str) -> int:
    """Return the id of a project's storage location (it must exist)."""
    return session.scalars(
        select(StorageLocation.id)
        .join(Project)
        .where(Project.name == project, StorageLocation.name == name)
    ).one()


def add_inactive_storage_location(session: Session, project: str, name: str) -> int:
    """Add a storage location row that is no longer in config (``active = false``)."""
    project_id = session.scalars(select(Project.id).where(Project.name == project)).one()
    row = StorageLocation(project_id=project_id, name=name, url=f"memory://{name}", active=False)
    session.add(row)
    session.flush()
    return row.id


def add_file(
    session: Session,
    project: str,
    path: str,
    *,
    copies: Mapping[str, FileState] | None = None,
    size: int = 1,
    minute: int = 0,
    latest: bool = True,
    deleted: bool = False,
    ignored: bool = False,
) -> int:
    """Insert one file (in a series of its own) and its copies; return the file id.

    Args:
        session: a write session; the caller commits.
        project: an existing project.
        path: relative path.
        copies: storage location name to copy state.
        size: file size in bytes.
        minute: ``created_at`` is this many minutes after :data:`T0`.
        latest: ``is_latest``.
        deleted: set ``deleted_at``.
        ignored: ``ignored_from_all_rules``.
    """
    project_id = session.scalars(select(Project.id).where(Project.name == project)).one()
    key = path + ("#deleted" if deleted else "") + f"#{minute}"
    series = Series(project_id=project_id, series_key=key)
    session.add(series)
    session.flush()
    created = T0 + timedelta(minutes=minute)
    file = File(
        project_id=project_id,
        path=path,
        name=posixpath.basename(path),
        series_id=series.id,
        size=size,
        hash="0" * 32,
        hash_algo="md5",
        is_latest=latest,
        ignored_from_all_rules=ignored,
        created_at=created,
        updated_at=created,
        deleted_at=created if deleted else None,
    )
    session.add(file)
    session.flush()
    for name, state in (copies or {}).items():
        session.add(
            FileStorageLocation(
                file_id=file.id,
                storage_location_id=storage_location_id(session, project, name),
                state=state,
                generation=0,
            )
        )
    session.flush()
    return file.id


_A, _P, _OV, _M, _D, _X, _U = (
    FileState.AVAILABLE,
    FileState.PENDING,
    FileState.OVERWRITING,
    FileState.MISSING,
    FileState.DELETING,
    FileState.DELETED,
    FileState.UNKNOWN,
)


def states(**counts: int) -> dict[str, int]:
    """Every state with zero, overridden by ``counts``."""
    return {state.value: 0 for state in FileState} | counts


def add_summary_dataset(session: Session, *, other: str, other_location: str) -> None:
    """Insert the summary golden dataset (see :data:`SUMMARY_EXPECTED`).

    ``demo`` gets copies in every state, a deleted file, an ignored file and an
    inactive storage location ``old``; one file in project ``other`` (in its
    storage location ``other_location``) must not be counted.
    """
    add_inactive_storage_location(session, "demo", "old")
    add_file(session, "demo", "a.txt", size=10, copies={"primary": _A, "secondary": _P})
    add_file(
        session, "demo", "b.txt", size=20, latest=False, copies={"primary": _M, "secondary": _X}
    )
    add_file(session, "demo", "c.txt", size=30, copies={"primary": _U, "old": _A})
    add_file(session, "demo", "d.txt", size=40, ignored=True, copies={"secondary": _OV})
    add_file(
        session, "demo", "e.txt", size=100, latest=False, deleted=True,
        copies={"primary": _X, "secondary": _A},
    )  # fmt: skip
    add_file(session, "demo", "f.txt", size=5, copies={"primary": _D})
    add_file(session, other, "o.txt", size=1000, copies={other_location: _A})


SUMMARY_EXPECTED: dict[str, Any] = {
    "project": "demo",
    "files": 5,
    "latest_files": 4,
    "bytes": 105,
    "deleted_files": 1,
    "storage_locations": [
        {
            "name": "primary",
            "active": True,
            "files": 4,
            "bytes": 65,
            "states": states(available=1, missing=1, unknown=1, deleting=1),
            "no_copy": 1,
        },
        {
            "name": "secondary",
            "active": True,
            "files": 2,
            "bytes": 50,
            "states": states(pending=1, deleted=1, overwriting=1),
            "no_copy": 3,
        },
        {
            "name": "old",
            "active": False,
            "files": 1,
            "bytes": 30,
            "states": states(available=1),
            "no_copy": 4,
        },
    ],
    "views": {"needs_attention": 2, "in_progress": 3, "ignored": 1},
}

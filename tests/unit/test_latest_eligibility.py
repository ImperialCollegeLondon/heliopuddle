"""Latest eligibility (API-12, D3): only live files with a pending/available copy can be latest."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from heliopuddle.server import models
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import File, Series
from heliopuddle.server.schemas import FileState

S = FileState


def _add(ctx: AppContext, path: str, states: dict[str, FileState]) -> int:
    """Register ``path`` into each storage location of ``states`` and set the copy states."""
    file_id = 0
    for location, state in states.items():
        with ctx.write_session() as session:
            result = models.register_file(
                session, project="demo", storage_location=location, path=path, hash="h", size=1
            )
            if state != S.PENDING:
                models.set_copy_state(session, result.copy_id, state)
            file_id = result.file_id
    return file_id


def _latest(ctx: AppContext, file_id: int) -> tuple[set[int], int | None]:
    with ctx.sessionmaker() as session:
        series_id = session.scalars(select(File.series_id).where(File.id == file_id)).one()
        latest = set(
            session.scalars(
                select(File.id).where(File.series_id == series_id, File.is_latest.is_(True))
            )
        )
        return latest, session.get_one(Series, series_id).latest_file_id


@pytest.mark.req("API-12")
@pytest.mark.parametrize(
    ("v1_states", "v2_states", "expected"),
    [
        ({"primary": S.AVAILABLE}, {"primary": S.PENDING}, "v2"),
        ({"primary": S.AVAILABLE}, {"primary": S.AVAILABLE}, "v2"),
        ({"primary": S.AVAILABLE}, {"primary": S.UNKNOWN}, "v1"),
        ({"primary": S.AVAILABLE}, {"primary": S.MISSING}, "v1"),
        ({"primary": S.AVAILABLE}, {"primary": S.DELETING}, "v1"),
        ({"primary": S.AVAILABLE}, {"primary": S.DELETED}, "v1"),
        ({"primary": S.AVAILABLE}, {"primary": S.UNKNOWN, "secondary": S.AVAILABLE}, "v2"),
        ({"primary": S.AVAILABLE}, {"primary": S.MISSING, "secondary": S.PENDING}, "v2"),
        ({"primary": S.UNKNOWN}, {"primary": S.UNKNOWN, "secondary": S.MISSING}, None),
        ({"primary": S.PENDING}, {"primary": S.UNKNOWN}, "v1"),
        ({"primary": S.MISSING}, {"primary": S.DELETING}, None),
    ],
)
def test_latest_considers_only_eligible_files(
    ctx: AppContext,
    v1_states: dict[str, FileState],
    v2_states: dict[str, FileState],
    expected: str | None,
) -> None:
    ids = {"v1": _add(ctx, "e/x_v1.dat", v1_states), "v2": _add(ctx, "e/x_v2.dat", v2_states)}
    latest, latest_file_id = _latest(ctx, ids["v1"])
    if expected is None:
        assert latest == set()
        assert latest_file_id is None
    else:
        assert latest == {ids[expected]}
        assert latest_file_id == ids[expected]


@pytest.mark.req("API-12")
def test_state_changes_move_latest_back_and_forth(ctx: AppContext) -> None:
    v1 = _add(ctx, "m/y_v1.dat", {"primary": S.AVAILABLE})
    with ctx.write_session() as session:
        v2 = models.register_file(
            session, project="demo", storage_location="primary", path="m/y_v2.dat", hash="h", size=1
        )
    assert _latest(ctx, v1) == ({v2.file_id}, v2.file_id)

    # A failed verify (unknown) makes v2 ineligible: v1 is latest again.
    with ctx.write_session() as session:
        models.set_copy_state(session, v2.copy_id, S.UNKNOWN)
    assert _latest(ctx, v1) == ({v1}, v1)

    # Verified later (available): v2 is latest again.
    with ctx.write_session() as session:
        models.set_copy_verified(session, v2.copy_id, size=1, mtime=None, hash="h")
    assert _latest(ctx, v1) == ({v2.file_id}, v2.file_id)

    # Removing v2: once its copies are deleting, v1 is latest.
    with ctx.write_session() as session:
        models.begin_delete(session, v2.file_id)
    assert _latest(ctx, v1) == ({v1}, v1)


@pytest.mark.req("API-12")
def test_series_with_no_eligible_file_has_no_latest(ctx: AppContext) -> None:
    only = _add(ctx, "n/z_v3.dat", {"primary": S.UNKNOWN})
    assert _latest(ctx, only) == (set(), None)
    with ctx.sessionmaker() as session:
        file = models.get_file(session, only)
        assert file is not None
        assert models.to_file_read(file).is_latest is False

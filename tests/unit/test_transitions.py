"""Guarded copy transitions: ownership, eligibility and expectations (STO-09, SVC-11)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from heliopuddle.server import models
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import File, FileStorageLocation
from heliopuddle.server.models import transitions as tr
from heliopuddle.server.schemas import FileState

S = FileState
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _copy(**fields: object) -> FileStorageLocation:
    copy = FileStorageLocation(state=S.PENDING, generation=0)
    for key, value in fields.items():
        setattr(copy, key, value)
    return copy


def _register(ctx: AppContext, path: str = "t/a.dat") -> models.RegisterResult:
    with ctx.write_session() as session:
        return models.register_file(
            session, project="demo", storage_location="primary", path=path, hash="h", size=1
        )


@pytest.mark.req("STO-09")
def test_is_owned_needs_a_token_and_a_future_deadline() -> None:
    assert not tr.is_owned(_copy(), NOW)
    assert not tr.is_owned(_copy(op_token="t"), NOW)
    assert tr.is_owned(_copy(op_token="t", op_deadline=NOW + timedelta(seconds=1)), NOW)
    assert not tr.is_owned(_copy(op_token="t", op_deadline=NOW), NOW)
    assert tr.is_expired(_copy(op_token="t", op_deadline=NOW), NOW)
    assert not tr.is_expired(_copy(), NOW)


@pytest.mark.req("STO-09")
@pytest.mark.parametrize(
    ("state", "token", "eligible"),
    [
        (S.AVAILABLE, None, True),
        (S.OVERWRITING, "t", True),
        (S.PENDING, None, True),
        (S.PENDING, "t", False),
        (S.UNKNOWN, None, False),
        (S.MISSING, None, False),
        (S.DELETING, "t", False),
        (S.DELETING, None, False),
        (S.DELETED, None, False),
    ],
)
def test_latest_eligibility(state: FileState, token: str | None, eligible: bool) -> None:
    copy = _copy(state=state, op_token=token, op_deadline=NOW - timedelta(hours=1))
    assert tr.is_latest_eligible(copy, NOW) is eligible


@pytest.mark.req("STO-09")
@pytest.mark.req("SVC-11")
def test_lock_copy_checks_every_expectation(ctx: AppContext) -> None:
    reg = _register(ctx)
    with ctx.write_session() as session:
        copy = tr.lock_copy(
            session, reg.copy_id, generation=0, states={S.PENDING}, expected_hash="H"
        )
        assert copy.id == reg.copy_id
        tr.bump(copy)

    failing: list[dict[str, object]] = [
        {"generation": 0},
        {"states": {S.AVAILABLE}},
        {"expected_hash": "other"},
        {"token": "not-mine"},
    ]
    for expectation in failing:
        with pytest.raises(tr.StaleOperation), ctx.write_session() as session:
            tr.lock_copy(session, reg.copy_id, **expectation)  # type: ignore[arg-type]

    with pytest.raises(tr.StaleOperation, match="no longer exists"), ctx.write_session() as s:
        tr.lock_copy(s, 9999)


@pytest.mark.req("STO-09")
def test_lock_copy_ownership(ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    reg = _register(ctx)
    monkeypatch.setattr(tr, "clock", lambda: NOW)
    with ctx.write_session() as session:
        copy = tr.lock_copy(session, reg.copy_id)
        tr.take(copy, S.PENDING, "tok", NOW + timedelta(minutes=5))

    # Someone else, while the token is live: busy, with the deadline.
    with pytest.raises(tr.CopyBusy) as e, ctx.write_session() as session:
        tr.lock_copy(session, reg.copy_id)
    assert e.value.deadline == NOW + timedelta(minutes=5)
    assert e.value.copy_id == reg.copy_id
    with pytest.raises(tr.CopyBusy), ctx.write_session() as session:
        tr.lock_copy(session, reg.copy_id, token="other")
    # The owner itself passes.
    with ctx.write_session() as session:
        assert tr.lock_copy(session, reg.copy_id, token="tok").op_token == "tok"

    # Once expired, nobody but the token holder passes, and others are not busy.
    monkeypatch.setattr(tr, "clock", lambda: NOW + timedelta(hours=1))
    with pytest.raises(tr.StaleOperation, match="expired"), ctx.write_session() as session:
        tr.lock_copy(session, reg.copy_id)
    with ctx.write_session() as session:
        copy = tr.lock_copy(session, reg.copy_id, token="tok")
        tr.release(copy, S.AVAILABLE)
    with ctx.sessionmaker() as session:
        copy = session.get_one(FileStorageLocation, reg.copy_id)
        assert (copy.state, copy.op_token, copy.op_deadline) == (S.AVAILABLE, None, None)
    # The released token is no longer current.
    with pytest.raises(tr.StaleOperation, match="no longer current"), ctx.write_session() as s:
        tr.lock_copy(s, reg.copy_id, token="tok")


@pytest.mark.req("SVC-11")
def test_lock_copy_requires_a_live_file(ctx: AppContext) -> None:
    reg = _register(ctx)
    with ctx.write_session() as session:
        session.get_one(File, reg.file_id).deleted_at = NOW
    with pytest.raises(tr.StaleOperation, match="deleted"), ctx.write_session() as session:
        tr.lock_copy(session, reg.copy_id)
    with ctx.write_session() as session:
        assert tr.lock_copy(session, reg.copy_id, require_live=False).id == reg.copy_id

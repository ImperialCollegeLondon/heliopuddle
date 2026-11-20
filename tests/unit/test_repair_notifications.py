# ruff: noqa: F811
"""Expired-operation repairs notify once, after their terminal state commits."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta
from unittest.mock import Mock

import pytest

from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import File
from heliopuddle.server.models import transfers, uploads
from heliopuddle.server.models.snapshots import CopySnapshot, snapshot
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services import upload as upload_service
from heliopuddle.server.services.upload import RepairResult
from tests.adversarial import (
    Clock,
    assert_invariants,
    clock,  # noqa: F401 - fixture
    config_dir,  # noqa: F401 - fixture
    protocol,  # noqa: F401 - fixture
    put,
    store,
)
from tests.lifecycle import md5, patch_upload
from tests.unit._transfer_helpers import DATA, PATH, copy, upload


@pytest.fixture
def notifications(monkeypatch: pytest.MonkeyPatch) -> Mock:
    boundary = Mock()
    patch_upload(monkeypatch, "_notify_file_changes", boundary)
    return boundary


def _pending(ctx: AppContext, kind: str = "pending") -> tuple[CopySnapshot, str, bytes]:
    data = b"the published replacement" if kind == "overwrite" else DATA
    if kind == "overwrite":
        upload(ctx)
    elif kind == "staged":
        put(store(ctx), PATH, b"an existing unregistered object")
    present = upload_service.object_present(ctx, "demo", "primary", PATH)
    with ctx.write_session() as session:
        reservation = uploads.reserve(
            session,
            project="demo",
            storage_location="primary",
            path=PATH,
            hash=md5(data),
            size=len(data),
            lease=ctx.settings.op_lease,
            final_exists=present,
            overwrite=kind == "overwrite",
        )
    assert reservation.token is not None
    assert reservation.write_path is not None
    put(store(ctx), reservation.write_path, data)
    if kind == "overwrite":
        put(store(ctx), PATH, data)
    return copy(ctx), reservation.token, data


@pytest.mark.req("SVC-24")
@pytest.mark.parametrize("kind", ["pending", "staged", "overwrite"])
def test_repair_publication_notifies_once_after_content_and_latest_commit(
    ctx: AppContext, clock: Clock, notifications: Mock, kind: str
) -> None:
    owned, token, data = _pending(ctx, kind)
    observed: list[tuple[FileState, str, str | None, bool]] = []

    def notify(_ctx: AppContext, _project: str, _file_ids: Iterable[int]) -> None:
        with ctx.sessionmaker() as session:
            snap = snapshot(session, owned.copy_id)
            assert snap is not None
            file = session.get_one(File, owned.file_id)
            observed.append((snap.state, file.hash, snap.token, file.is_latest))

    notifications.side_effect = notify
    clock.advance(ctx.settings.op_lease + timedelta(seconds=1))
    assert upload_service.repair_expired(owned.copy_id) == RepairResult.REPAIRED
    notifications.assert_called_once_with(ctx, "demo", [owned.file_id])
    assert observed == [(FileState.AVAILABLE, md5(data), None, True)]
    assert upload_service.repair_expired(owned.copy_id) == RepairResult.NOT_OWNED_OR_LIVE
    assert upload_service.complete_upload(token).state == FileState.AVAILABLE
    notifications.assert_called_once()
    assert_invariants(ctx)


@pytest.mark.req("SVC-24")
@pytest.mark.parametrize("kind", ["drop", "delete"])
def test_repair_terminal_state_changes_notify_after_commit(
    ctx: AppContext, clock: Clock, notifications: Mock, kind: str
) -> None:
    if kind == "drop":
        owned, _token, _data = _pending(ctx)
        put(store(ctx), PATH, DATA[:4])
    else:
        upload(ctx)
        before = copy(ctx)
        with ctx.write_session() as session:
            owned = transfers.begin_source_delete(
                session, before, store(ctx).url, ctx.settings.op_lease
            )
    observed: list[FileState | None] = []

    def notify(_ctx: AppContext, _project: str, _file_ids: Iterable[int]) -> None:
        with ctx.sessionmaker() as session:
            snap = snapshot(session, owned.copy_id)
            observed.append(snap.state if snap is not None else None)

    notifications.side_effect = notify
    clock.advance(ctx.settings.op_lease + timedelta(seconds=1))
    expected = RepairResult.DROPPED if kind == "drop" else RepairResult.REPAIRED
    assert upload_service.repair_expired(owned.copy_id) == expected
    notifications.assert_called_once_with(ctx, "demo", [owned.file_id])
    assert observed == [None if kind == "drop" else FileState.DELETED]
    assert_invariants(ctx)


@pytest.mark.req("SVC-24")
@pytest.mark.parametrize("error", [ModuleNotFoundError, RuntimeError])
def test_repair_notification_failure_keeps_successful_repair_result(
    ctx: AppContext,
    clock: Clock,
    notifications: Mock,
    caplog: pytest.LogCaptureFixture,
    error: type[Exception],
) -> None:
    owned, _token, _data = _pending(ctx)
    notifications.side_effect = error("repair notification unavailable")
    clock.advance(ctx.settings.op_lease + timedelta(seconds=1))
    assert upload_service.repair_expired(owned.copy_id) == RepairResult.REPAIRED
    assert copy(ctx).state == FileState.AVAILABLE
    notifications.assert_called_once_with(ctx, "demo", [owned.file_id])
    assert "repair notification unavailable" in caplog.text
    assert_invariants(ctx)


@pytest.mark.req("SVC-24")
@pytest.mark.parametrize("reason", ["live", "missing-config"])
def test_noop_or_incomplete_repairs_do_not_notify(
    ctx: AppContext, clock: Clock, notifications: Mock, reason: str
) -> None:
    owned, _token, _data = _pending(ctx)
    if reason == "missing-config":
        clock.advance(ctx.settings.op_lease + timedelta(seconds=1))
        ctx.projects["demo"].storage_locations.clear()
    result = upload_service.repair_expired(owned.copy_id)
    assert result == (
        RepairResult.NOT_OWNED_OR_LIVE if reason == "live" else RepairResult.INCOMPLETE
    )
    notifications.assert_not_called()

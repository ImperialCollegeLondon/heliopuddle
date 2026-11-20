"""Transfer notifications observe committed state without retriggering the source."""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock, call

import pytest
from sqlalchemy import select

from heliopuddle.config.models import RuleOptions
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import File, FileStorageLocation
from heliopuddle.server.models import transitions as tr
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services import transfer
from tests.adversarial import (
    assert_invariants,
    config_dir,  # noqa: F401 - fixture
    protocol,  # noqa: F401 - fixture
    put,
    store,
)
from tests.unit._transfer_helpers import (
    DATA,
    PATH,
    copy,
    limits,  # noqa: F401 - fixture
    upload,
)


@pytest.fixture
def notifications(monkeypatch: pytest.MonkeyPatch) -> Mock:
    boundary = Mock()
    monkeypatch.setattr(transfer, "_notify_file_changes", boundary, raising=False)
    return boundary


def _states(ctx: AppContext, file_id: int) -> dict[str, FileState]:
    with ctx.sessionmaker() as session:
        return {
            copy.storage_location.name: copy.state
            for copy in session.scalars(
                select(FileStorageLocation).where(FileStorageLocation.file_id == file_id)
            )
        }


@pytest.mark.req("SVC-19", "SVC-24")
@pytest.mark.parametrize("existing_object", [False, True])
def test_copy_notifies_only_destination_after_commit(
    ctx: AppContext, notifications: Mock, existing_object: bool
) -> None:
    file_id = upload(ctx)
    if existing_object:
        put(store(ctx, "secondary"), PATH, DATA)
    observed: list[dict[str, FileState]] = []

    def notify(
        _ctx: AppContext,
        _project: str,
        _file_ids: Iterable[int],
        *,
        storage_locations: Collection[str] | None = None,
    ) -> None:
        observed.append(_states(ctx, file_id))

    notifications.side_effect = notify
    assert transfer.transfer_file.fn(file_id, "primary", "secondary").outcome == "copied"
    notifications.assert_called_once_with(ctx, "demo", [file_id], storage_locations={"secondary"})
    assert observed == [{"primary": FileState.AVAILABLE, "secondary": FileState.AVAILABLE}]
    assert_invariants(ctx)


@pytest.mark.req("SVC-19", "SVC-24")
@pytest.mark.parametrize("source_changes", [False, True])
def test_move_notifies_committed_destination_before_attempting_source_delete(
    ctx: AppContext, notifications: Mock, source_changes: bool
) -> None:
    file_id = upload(ctx)
    observed: list[dict[str, FileState]] = []

    def notify(
        _ctx: AppContext,
        _project: str,
        _file_ids: Iterable[int],
        *,
        storage_locations: Collection[str] | None = None,
    ) -> None:
        observed.append(_states(ctx, file_id))
        if source_changes and len(observed) == 1:
            upload(ctx, b"a newly committed source revision", overwrite=True)

    notifications.side_effect = notify
    if source_changes:
        with pytest.raises(tr.StaleOperation):
            transfer.transfer_file.fn(file_id, "primary", "secondary", action="move")
    else:
        assert (
            transfer.transfer_file.fn(file_id, "primary", "secondary", action="move").outcome
            == "moved"
        )
    expected = [{"primary": FileState.AVAILABLE, "secondary": FileState.AVAILABLE}]
    if not source_changes:
        expected.append({"primary": FileState.DELETED, "secondary": FileState.AVAILABLE})
    assert observed == expected
    assert notifications.call_args_list == [
        call(ctx, "demo", [file_id], storage_locations=None)
    ] * len(expected)
    assert_invariants(ctx)


@pytest.mark.req("SVC-20", "SVC-24")
@pytest.mark.parametrize("has_archive", [False, True])
def test_source_delete_notifies_unrestricted_after_committed_latest_result(
    ctx: AppContext, notifications: Mock, has_archive: bool
) -> None:
    file_id = upload(ctx)
    if has_archive:
        upload(ctx, storage_location="secondary")
    observed: list[tuple[dict[str, FileState], bool, bool]] = []

    def notify(
        _ctx: AppContext,
        _project: str,
        _file_ids: Iterable[int],
        *,
        storage_locations: Collection[str] | None = None,
    ) -> None:
        with ctx.sessionmaker() as session:
            file = session.get_one(File, file_id)
            observed.append((_states(ctx, file_id), file.deleted_at is not None, file.is_latest))

    notifications.side_effect = notify
    assert transfer.delete_source_copy.fn(file_id, "primary").outcome == "deleted"
    notifications.assert_called_once_with(ctx, "demo", [file_id], storage_locations=None)
    states = {"primary": FileState.DELETED}
    if has_archive:
        states["secondary"] = FileState.AVAILABLE
    assert observed == [(states, not has_archive, has_archive)]
    assert_invariants(ctx)


@pytest.mark.req("SVC-19", "SVC-20", "SVC-24")
@pytest.mark.parametrize("action", ["copy", "move", "delete"])
@pytest.mark.parametrize("error", [ModuleNotFoundError, RuntimeError])
def test_notification_failure_never_fails_a_committed_action(
    ctx: AppContext,
    notifications: Mock,
    caplog: pytest.LogCaptureFixture,
    action: str,
    error: type[Exception],
) -> None:
    file_id = upload(ctx)
    notifications.side_effect = error("notification dispatcher is down")
    if action == "delete":
        result = transfer.delete_source_copy.fn(file_id, "primary")
    else:
        result = transfer.transfer_file.fn(file_id, "primary", "secondary", action=action)
    assert result.outcome == {"copy": "copied", "move": "moved", "delete": "deleted"}[action]
    assert notifications.call_count == (2 if action == "move" else 1)
    assert "notification dispatcher is down" in caplog.text
    assert_invariants(ctx)


@pytest.mark.req("SVC-19", "SVC-24")
@pytest.mark.parametrize("action", ["copy", "move"])
@pytest.mark.parametrize("existing_data", [None, DATA, b"conflicting data"])
def test_dry_runs_do_not_notify(
    ctx: AppContext, notifications: Mock, action: str, existing_data: bytes | None
) -> None:
    file_id = upload(ctx)
    if existing_data is not None:
        put(store(ctx, "secondary"), PATH, existing_data)
    transfer.transfer_file.fn(
        file_id, "primary", "secondary", action=action, options=RuleOptions(dry_run=True)
    )
    notifications.assert_not_called()
    assert_invariants(ctx)


@pytest.mark.req("SVC-19", "SVC-24")
@pytest.mark.parametrize("newer_only", [False, True])
def test_skips_notify_only_when_destination_observation_commits(
    ctx: AppContext, notifications: Mock, newer_only: bool
) -> None:
    file_id = upload(ctx)
    destination = store(ctx, "secondary")
    put(destination, PATH, b"conflicting destination")
    mtime = copy(ctx).mtime
    assert mtime is not None
    with destination.open_read(PATH) as stream:
        stream.created = mtime + timedelta(days=1)  # type: ignore[attr-defined]
    observed: list[dict[str, FileState]] = []

    def notify(
        _ctx: AppContext,
        _project: str,
        _file_ids: Iterable[int],
        *,
        storage_locations: Collection[str] | None = None,
    ) -> None:
        observed.append(_states(ctx, file_id))

    notifications.side_effect = notify
    result = transfer.transfer_file.fn(
        file_id,
        "primary",
        "secondary",
        options=RuleOptions(update_only_if_newer=newer_only),
    )
    assert result.outcome == ("skipped" if newer_only else "conflict")
    if newer_only:
        notifications.assert_not_called()
        assert observed == []
    else:
        notifications.assert_called_once_with(
            ctx, "demo", [file_id], storage_locations={"secondary"}
        )
        assert observed == [{"primary": FileState.AVAILABLE, "secondary": FileState.UNKNOWN}]
    assert_invariants(ctx)


@pytest.mark.req("SVC-06")
@pytest.mark.parametrize("action", ["copy", "move"])
def test_identical_available_destination_writes_and_notifies_nothing_extra(
    ctx: AppContext, notifications: Mock, action: str
) -> None:
    file_id = upload(ctx)
    upload(ctx, storage_location="secondary")
    destination = copy(ctx, "secondary")
    result = transfer.transfer_file.fn(
        file_id,
        "primary",
        "secondary",
        action=action,  # type: ignore[arg-type]
    )
    if action == "copy":
        assert copy(ctx, "secondary") == destination
        assert result.outcome == "unchanged"
        assert copy(ctx).state == FileState.AVAILABLE
        notifications.assert_not_called()
    else:
        # Nothing written; the arrival reset bumps the generation only (SVC-29).
        assert copy(ctx, "secondary") == replace(destination, generation=destination.generation + 1)
        assert result.outcome == "moved"
        assert copy(ctx).state == FileState.DELETED
        notifications.assert_called_once()  # only the source delete
    assert_invariants(ctx)


@pytest.mark.req("SVC-06")
def test_repeating_a_recorded_conflict_is_a_silent_skip(
    ctx: AppContext, notifications: Mock
) -> None:
    file_id = upload(ctx)
    put(store(ctx, "secondary"), PATH, b"conflicting destination")
    assert transfer.transfer_file.fn(file_id, "primary", "secondary").outcome == "conflict"
    assert notifications.call_count == 1
    recorded = copy(ctx, "secondary")
    again = transfer.transfer_file.fn(file_id, "primary", "secondary")
    assert again.outcome == "skipped"
    assert copy(ctx, "secondary") == recorded
    assert notifications.call_count == 1
    assert_invariants(ctx)

"""When a file arrived in a storage location, and the cooling-off it gives rules (SVC-29)."""

from __future__ import annotations

import hashlib
import io
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, update

from heliopuddle.config.models import RuleConfig, RuleOptions, RuleSpec
from heliopuddle.server import models
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import File, FileStorageLocation, RuleState, utcnow
from heliopuddle.server.models import transitions as tr
from heliopuddle.server.models import uploads
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services import rule_checkpoint, transfer
from heliopuddle.server.services import rules as rule_service
from heliopuddle.server.services.accept import accept_copy
from heliopuddle.server.services.index import index_full
from heliopuddle.server.services.stores import store_for
from heliopuddle.server.services.upload import complete_upload, object_present
from heliopuddle.server.services.verify import verify_file

pytestmark = pytest.mark.usefixtures("inline_prefect")

PATH = "data/a.dat"
DATA = b"the arriving content"
OTHER = b"some other content"
# A sentinel arrival: an operation that keeps arrived_at leaves exactly this.
LONG_AGO = datetime(2020, 1, 1, tzinfo=UTC)


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def put(ctx: AppContext, location: str, path: str, data: bytes) -> None:
    """Write an object the way another tool would, settled (mtime two hours ago)."""
    loc = store_for(ctx, "demo", location)
    target = Path(loc.full_path(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    when = (utcnow() - timedelta(hours=2)).timestamp()
    os.utime(target, (when, when))


def upload(
    ctx: AppContext,
    data: bytes = DATA,
    location: str = "primary",
    *,
    overwrite: bool = False,
    before_complete: Callable[[int], None] | None = None,
) -> int:
    """Upload through the real reserve and complete protocol."""
    present = object_present(ctx, "demo", location, PATH)
    with ctx.write_session() as session:
        reservation = uploads.reserve(
            session,
            project="demo",
            storage_location=location,
            path=PATH,
            hash=md5(data),
            size=len(data),
            lease=ctx.settings.op_lease,
            final_exists=present,
            overwrite=overwrite,
        )
    assert reservation.token is not None and reservation.write_path is not None
    loc = store_for(ctx, "demo", location)
    loc.write(reservation.write_path, io.BytesIO(data))
    if before_complete is not None:
        assert reservation.copy_id is not None
        before_complete(reservation.copy_id)
    assert complete_upload(reservation.token).state == FileState.AVAILABLE
    return reservation.file_id


def copy_row(ctx: AppContext, location: str, path: str = PATH) -> FileStorageLocation:
    with ctx.sessionmaker() as session:
        row = next(
            c
            for c in session.scalars(select(FileStorageLocation))
            if c.storage_location.name == location and c.file.path == path
        )
        session.expunge(row)
        return row


def arrived(ctx: AppContext, location: str, path: str = PATH) -> datetime:
    return copy_row(ctx, location, path).arrived_at


def backdate(ctx: AppContext, location: str, path: str = PATH) -> None:
    """Set the copy's arrival to the sentinel, so a later reset (or none) is visible."""
    copy_id = copy_row(ctx, location, path).id
    with ctx.write_session() as session:
        session.execute(
            update(FileStorageLocation)
            .where(FileStorageLocation.id == copy_id)
            .values(arrived_at=LONG_AGO)
        )


def assert_recent(value: datetime, since: datetime) -> None:
    assert since <= value <= utcnow()


@pytest.fixture(autouse=True)
def limits(monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import nullcontext

    monkeypatch.setattr(transfer, "hp_concurrency", lambda *a, **k: nullcontext())
    monkeypatch.setattr(transfer, "ensure_concurrency_limit", lambda *_a: None)


# --- first arrival -------------------------------------------------------------------


@pytest.mark.req("SVC-29")
def test_register_sets_arrival_on_a_new_copy_only(ctx: AppContext) -> None:
    start = utcnow()
    with ctx.write_session() as session:
        result = models.register_file(
            session,
            project="demo",
            storage_location="primary",
            path=PATH,
            hash=md5(DATA),
            size=len(DATA),
        )
        models.set_copy_verified(
            session, result.copy_id, size=len(DATA), mtime=None, hash=md5(DATA)
        )
    assert_recent(arrived(ctx, "primary"), start)
    backdate(ctx, "primary")
    with ctx.write_session() as session:
        again = models.register_file(
            session,
            project="demo",
            storage_location="primary",
            path=PATH,
            hash=md5(DATA),
            size=len(DATA),
        )
    assert again.copy_id == result.copy_id
    assert arrived(ctx, "primary") == LONG_AGO


@pytest.mark.req("SVC-29")
def test_upload_arrives_when_it_completes_not_when_it_is_reserved(ctx: AppContext) -> None:
    marks: list[datetime] = []

    def reserved_long_ago(copy_id: int) -> None:
        with ctx.write_session() as session:
            session.get_one(FileStorageLocation, copy_id).arrived_at = LONG_AGO
        marks.append(utcnow())

    upload(ctx, before_complete=reserved_long_ago)
    assert_recent(arrived(ctx, "primary"), marks[0])


@pytest.mark.req("SVC-29")
def test_index_add_sets_arrival_and_reindex_keeps_it(ctx: AppContext) -> None:
    start = utcnow()
    put(ctx, "primary", PATH, DATA)
    assert index_full.fn("demo", "primary").added == 1
    assert_recent(arrived(ctx, "primary"), start)

    backdate(ctx, "primary")
    index_full.fn("demo", "primary")  # unchanged
    assert arrived(ctx, "primary") == LONG_AGO
    put(ctx, "primary", PATH, OTHER)  # changed in place: re-indexed, not a new arrival
    index_full.fn("demo", "primary")
    assert arrived(ctx, "primary") == LONG_AGO


# --- rule transfers ------------------------------------------------------------------


@pytest.mark.req("SVC-29")
@pytest.mark.parametrize("action", ["copy", "move"])
def test_copy_and_move_rules_set_arrival_in_the_destination(ctx: AppContext, action: str) -> None:
    file_id = upload(ctx)
    backdate(ctx, "primary")
    start = utcnow()
    result = transfer.transfer_file.fn(file_id, "primary", "secondary", action=action)  # type: ignore[arg-type]
    assert result.outcome == ("moved" if action == "move" else "copied")
    assert_recent(arrived(ctx, "secondary"), start)
    assert arrived(ctx, "primary") == LONG_AGO  # the source's arrival is untouched


@pytest.mark.req("SVC-29")
def test_a_rule_recording_an_identical_destination_sets_arrival(ctx: AppContext) -> None:
    file_id = upload(ctx)
    put(ctx, "secondary", PATH, DATA)  # already there, but not recorded
    start = utcnow()
    assert transfer.transfer_file.fn(file_id, "primary", "secondary").outcome == "copied"
    assert_recent(arrived(ctx, "secondary"), start)
    backdate(ctx, "secondary")
    # Already recorded as available: nothing arrives.
    assert transfer.transfer_file.fn(file_id, "primary", "secondary").outcome == "unchanged"
    assert arrived(ctx, "secondary") == LONG_AGO


@pytest.mark.req("SVC-29")
def test_a_move_onto_an_identical_recorded_destination_resets_arrival(ctx: AppContext) -> None:
    file_id = upload(ctx)
    put(ctx, "secondary", PATH, DATA)
    transfer.transfer_file.fn(file_id, "primary", "secondary")  # records it, copies nothing
    backdate(ctx, "secondary")
    start = utcnow()
    # The move writes nothing but leaves the file living in secondary: a new arrival.
    result = transfer.transfer_file.fn(file_id, "primary", "secondary", action="move")
    assert result.outcome == "moved"
    assert_recent(arrived(ctx, "secondary"), start)
    assert copy_row(ctx, "primary").state == FileState.DELETED


@pytest.mark.req("SVC-29")
def test_a_move_onto_identical_content_with_a_new_mtime_resets_arrival(ctx: AppContext) -> None:
    file_id = upload(ctx)
    put(ctx, "secondary", PATH, DATA)
    transfer.transfer_file.fn(file_id, "primary", "secondary")  # records it
    backdate(ctx, "secondary")
    # Same content, touched since it was recorded: the move re-records it.
    target = Path(store_for(ctx, "demo", "secondary").full_path(PATH))
    when = (utcnow() - timedelta(hours=1)).timestamp()
    os.utime(target, (when, when))
    start = utcnow()
    assert transfer.transfer_file.fn(file_id, "primary", "secondary", action="move").outcome == (
        "moved"
    )
    assert_recent(arrived(ctx, "secondary"), start)


@pytest.mark.req("SVC-29")
def test_an_arrival_reset_makes_earlier_delete_candidates_stale(ctx: AppContext) -> None:
    file_id = upload(ctx)
    put(ctx, "secondary", PATH, DATA)
    transfer.transfer_file.fn(file_id, "primary", "secondary")
    backdate(ctx, "secondary")
    # A delete of the destination selected while it was old enough...
    candidate = copy_row(ctx, "secondary").generation
    transfer.transfer_file.fn(file_id, "primary", "secondary", action="move")
    assert copy_row(ctx, "secondary").generation > candidate
    # ...must not run once the move has restarted its cooling-off period.
    with pytest.raises(tr.StaleOperation):
        transfer.delete_source_copy.fn(file_id, "secondary", expected_generation=candidate)
    assert copy_row(ctx, "secondary").state == FileState.AVAILABLE


@pytest.mark.req("SVC-29")
@pytest.mark.parametrize("previous", ["deleted", "unknown"])
def test_a_rule_reusing_an_old_destination_copy_resets_arrival(
    ctx: AppContext, previous: str
) -> None:
    file_id = upload(ctx)
    upload(ctx, location="secondary")
    if previous == "deleted":
        transfer.delete_source_copy.fn(file_id, "secondary")
    else:
        put(ctx, "secondary", PATH, OTHER)
        verify_file.fn(copy_row(ctx, "secondary").id)
    before = copy_row(ctx, "secondary")
    assert before.state == FileState(previous)
    backdate(ctx, "secondary")
    start = utcnow()
    assert (
        transfer.transfer_file.fn(
            file_id, "primary", "secondary", options=RuleOptions(overwrite=True)
        ).outcome
        == "copied"
    )
    after = copy_row(ctx, "secondary")
    assert (after.id, after.state) == (before.id, FileState.AVAILABLE)
    assert_recent(after.arrived_at, start)


# --- changes that are not arrivals ---------------------------------------------------


@pytest.mark.req("SVC-29")
def test_verify_accept_overwrite_metadata_and_content_date_keep_arrival(
    ctx: AppContext,
) -> None:
    file_id = upload(ctx)
    backdate(ctx, "primary")

    verify_file.fn(copy_row(ctx, "primary").id)
    assert arrived(ctx, "primary") == LONG_AGO

    upload(ctx, OTHER, overwrite=True)
    assert arrived(ctx, "primary") == LONG_AGO

    with ctx.write_session() as session:
        models.update_metadata(session, file_id, {"k": "v"})
    with ctx.write_session() as session:
        models.set_content_date(session, file_id, datetime(2026, 1, 2, tzinfo=UTC))
    assert arrived(ctx, "primary") == LONG_AGO

    put(ctx, "primary", PATH, DATA)  # changed outside hp: unknown, then accepted
    verify_file.fn(copy_row(ctx, "primary").id)
    assert copy_row(ctx, "primary").state == FileState.UNKNOWN
    accept_copy.fn(file_id, "primary")
    assert copy_row(ctx, "primary").state == FileState.AVAILABLE
    assert arrived(ctx, "primary") == LONG_AGO


@pytest.mark.req("SVC-29")
def test_the_api_read_exposes_arrival(ctx: AppContext) -> None:
    file_id = upload(ctx)
    with ctx.sessionmaker() as session:
        file = models.get_file(session, file_id)
        assert file is not None
        read = models.to_file_read(file)
    assert read.storage_locations[0].arrived_at == arrived(ctx, "primary")


# --- the cooling-off scenario --------------------------------------------------------


@pytest.mark.req("SVC-29", "SVC-04", "SVC-05")
@pytest.mark.parametrize("mode", ["full", "incremental"])
def test_a_file_moved_in_by_a_one_off_rule_cools_off_before_a_scheduled_rule_moves_it(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    file_id = upload(ctx)
    clock = [utcnow()]
    monkeypatch.setattr(rule_service, "utcnow", lambda: clock[0])
    monkeypatch.setattr(rule_checkpoint, "utcnow", lambda: clock[0])
    monkeypatch.setattr(rule_service, "get_run_logger", lambda: logging.getLogger("test"))
    monkeypatch.setattr(rule_service, "_schedule_successor", lambda *a, **k: None)
    monkeypatch.setattr(
        rule_service,
        "_execute_file",
        lambda candidate, spec: transfer.transfer_file.fn(
            candidate.file_id,
            spec.source,
            spec.destination,
            action=spec.action,
            options=spec.options,
            user="rule",
        ),
    )
    back = RuleConfig.model_validate(
        {
            "name": "back",
            "action": "move",
            "source": "secondary",
            "destination": "primary",
            "schedule": "0 * * * *",
            "match": {"paths": ["**"], "age": {"arrived": {"older_than": "1d"}}},
        }
    )
    ctx.projects["demo"].rules = [back]
    if mode == "incremental":
        # The scheduled rule has run before, so this is a real incremental run.
        with ctx.write_session() as session:
            session.add(
                RuleState(
                    project_id=1,
                    rule_name="back",
                    watermark_at=clock[0] - timedelta(hours=1),
                    watermark_id=0,
                    last_success_at=clock[0] - timedelta(hours=1),
                )
            )

    one_off = RuleSpec.model_validate(
        {
            "action": "move",
            "source": "primary",
            "destination": "secondary",
            "match": {"paths": ["**"]},
        }
    )
    assert rule_service.run_rule_once.fn("demo", one_off).outcomes == {"moved": 1}
    moved_at = arrived(ctx, "secondary")
    assert copy_row(ctx, "primary").state == FileState.DELETED

    def run(at: datetime) -> dict[str, int]:
        clock[0] = at
        return rule_service.run_rule.fn("demo", "back", mode=mode).outcomes  # type: ignore[arg-type]

    assert run(moved_at + timedelta(hours=1)) == {}
    assert run(moved_at + timedelta(hours=23, minutes=59)) == {}
    assert copy_row(ctx, "secondary").state == FileState.AVAILABLE
    assert run(moved_at + timedelta(days=1, minutes=1)) == {"moved": 1}
    assert copy_row(ctx, "primary").state == FileState.AVAILABLE
    assert copy_row(ctx, "secondary").state == FileState.DELETED
    with ctx.sessionmaker() as session:
        assert session.get_one(File, file_id).deleted_at is None

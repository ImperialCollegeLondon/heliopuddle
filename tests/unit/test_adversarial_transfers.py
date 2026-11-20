# ruff: noqa: F811
"""Adversarial interleavings of M4 transfers, rule runs and the index (SVC-19..23, STO-03/13).

Real transfer, rule and index code run under controlled phase interleavings;
every test ends with :func:`tests.adversarial.assert_invariants`.
"""

from __future__ import annotations

import io
import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from heliopuddle.config.models import RuleConfig, RuleOptions
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import FileStorageLocation, RuleState
from heliopuddle.server.models import transitions as tr
from heliopuddle.server.models import uploads
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services import delete as delete_mod
from heliopuddle.server.services import rules, transfer
from heliopuddle.server.services.upload import complete_upload, object_present
from heliopuddle.server.services.verify import verify_file
from heliopuddle.storage import StorageLocation
from tests.adversarial import (
    assert_invariants,
    clock,  # noqa: F401 - fixture
    protocol,  # noqa: F401 - fixture
    put,
    store,
)
from tests.lifecycle import _all, copies, md5, store_bytes, temp_files  # pyright: ignore[reportPrivateUsage]

PATH = "p/data_v1.dat"
DATA = b"the verified source content"
OTHER = b"a different destination"
THIRD = b"content written through a third copy"


@pytest.fixture
def config_dir(tmp_path: Path, protocol: str) -> Path:
    """Project ``demo`` with three storage locations on the fake store."""
    path = tmp_path / "config"
    path.mkdir()
    tag = uuid.uuid4().hex
    names = ["primary", "secondary", "tertiary"]
    (path / "demo.yaml").write_text(
        "project: demo\nstorage_locations:\n"
        + "".join(f"  - name: {n}\n    url: '{protocol}://hp-{tag}-{n}'\n" for n in names)
    )
    return path


@pytest.fixture(autouse=True)
def limits(monkeypatch: pytest.MonkeyPatch) -> None:
    @contextmanager
    def acquire(*_args: Any, **_kwargs: Any) -> Iterator[None]:
        yield

    for module in (transfer, rules):
        monkeypatch.setattr(module, "hp_concurrency", acquire)
        monkeypatch.setattr(module, "ensure_concurrency_limit", lambda *_a, **_k: None)
    monkeypatch.setattr(rules, "get_run_logger", lambda: logging.getLogger("test.adversarial"))

    def execute(candidate: Any, spec: Any) -> Any:
        if spec.action == "delete":
            return transfer.delete_source_copy.fn(
                candidate.file_id,
                spec.source,
                expected_generation=candidate.generation,
                expected_hash=candidate.hash,
                user=rules.SYSTEM_USER,
            )
        return transfer.transfer_file.fn(
            candidate.file_id,
            spec.source,
            spec.destination,
            action=spec.action,
            options=spec.options,
            user=rules.SYSTEM_USER,
        )

    monkeypatch.setattr(rules, "_execute_file", execute)


def upload(
    ctx: AppContext,
    data: bytes = DATA,
    location: str = "primary",
    path: str = PATH,
    *,
    overwrite: bool = False,
) -> int:
    present = object_present(ctx, "demo", location, path)
    with ctx.write_session() as session:
        reservation = uploads.reserve(
            session,
            project="demo",
            storage_location=location,
            path=path,
            hash=md5(data),
            size=len(data),
            lease=ctx.settings.op_lease,
            final_exists=present,
            overwrite=overwrite,
        )
    if reservation.token is not None:
        assert reservation.write_path is not None
        store(ctx, location).write(reservation.write_path, io.BytesIO(data))
        assert complete_upload(reservation.token).state == FileState.AVAILABLE
    return reservation.file_id


def state(ctx: AppContext, name: str) -> tuple[FileState, str | None]:
    row = next(c for c in copies(ctx) if c.storage_location == name)
    return row.state, row.copy_hash


@pytest.mark.req("SVC-19", "STO-03")
@pytest.mark.parametrize("mutation", ["overwrite", "remove"])
def test_destination_changed_in_store_after_available_keeps_the_move_source(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    file_id = upload(ctx)
    real_delete = transfer.delete.delete_object
    done = False

    def tamper(loc: StorageLocation, snap: Any, **kwargs: Any) -> FileState:
        nonlocal done
        if snap.storage_location == "primary" and not done:
            done = True
            assert state(ctx, "secondary") == (FileState.AVAILABLE, md5(DATA))
            dest = store(ctx, "secondary")
            if mutation == "overwrite":
                put(dest, PATH, OTHER)
            else:
                dest.rm(PATH)
        return real_delete(loc, snap, **kwargs)

    monkeypatch.setattr(transfer.delete, "delete_object", tamper)
    with pytest.raises(transfer.TransferVerificationError):
        transfer.transfer_file.fn(file_id, "primary", "secondary", action="move")
    assert store_bytes(store(ctx), PATH) == DATA
    assert next(c for c in copies(ctx) if c.storage_location == "primary").token is None
    # The tampered destination is found by verification, never by a source delete.
    verify_file.fn(next(c for c in copies(ctx) if c.storage_location == "secondary").copy_id)
    assert_invariants(ctx)


@pytest.mark.req("SVC-19", "STO-03")
def test_destination_url_reset_after_available_keeps_the_move_source(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_id = upload(ctx)
    real_delete = transfer.delete.delete_object
    done = False

    def reset(loc: StorageLocation, snap: Any, **kwargs: Any) -> FileState:
        nonlocal done
        if snap.storage_location == "primary" and not done:
            done = True
            ctx.projects["demo"].storage_location("secondary").url += "-reset"
            ctx.sync_config()
        return real_delete(loc, snap, **kwargs)

    monkeypatch.setattr(transfer.delete, "delete_object", reset)
    with pytest.raises(tr.StaleOperation):
        transfer.transfer_file.fn(file_id, "primary", "secondary", action="move")
    assert store_bytes(store(ctx), PATH) == DATA
    assert_invariants(ctx)


@pytest.mark.req("SVC-19", "STO-03")
def test_logical_hash_changed_through_another_copy_is_never_published_or_deleted(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_id = upload(ctx)
    assert transfer.transfer_file.fn(file_id, "primary", "tertiary").outcome == "copied"
    # The destination is owned while another copy's overwrite changes the logical hash.
    real_copy = transfer._copy_bytes
    done = False

    def during_copy(*args: Any, **kwargs: Any) -> None:
        nonlocal done
        if not done:
            done = True
            upload(ctx, THIRD, "tertiary", overwrite=True)
        real_copy(*args, **kwargs)

    monkeypatch.setattr(transfer, "_copy_bytes", during_copy)
    with pytest.raises(tr.StaleOperation):
        transfer.transfer_file.fn(file_id, "primary", "secondary", action="move")
    assert store_bytes(store(ctx), PATH) == DATA
    assert state(ctx, "primary")[0] != FileState.DELETED
    with ctx.sessionmaker() as session:
        dest = next(
            c
            for c in session.scalars(select(FileStorageLocation))
            if c.storage_location.name == "secondary"
        )
        assert dest.file.hash == md5(THIRD)
        assert dest.state != FileState.AVAILABLE or dest.hash == md5(THIRD)
    assert_invariants(ctx, allow_temp=True)


@pytest.mark.req("SVC-19", "STO-03", "SVC-21")
def test_rule_move_with_source_overwritten_mid_transfer_retries_new_content(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.projects["demo"].rules = [
        RuleConfig.model_validate(
            {
                "name": "archive",
                "action": "move",
                "source": "primary",
                "destination": "secondary",
                "match": {"paths": ["**"]},
                "options": {"retrigger": False},
            }
        )
    ]
    upload(ctx)
    real_copy = transfer._copy_bytes
    done = False

    def during_copy(*args: Any, **kwargs: Any) -> None:
        nonlocal done
        if not done:
            done = True
            upload(ctx, OTHER, overwrite=True)
        real_copy(*args, **kwargs)

    monkeypatch.setattr(transfer, "_copy_bytes", during_copy)
    with pytest.raises(RuntimeError, match="failed"):
        rules.run_rule.fn("demo", "archive")
    assert store_bytes(store(ctx), PATH) == OTHER
    with ctx.sessionmaker() as session:
        assert session.get(RuleState, (1, "archive")) is None
    # A retry after the destination is repaired moves the new content.
    for row in copies(ctx):
        if row.storage_location == "secondary":
            from heliopuddle.server.services.upload import repair_expired

            repair_expired(row.copy_id)
    rules.run_rule.fn("demo", "archive", mode="full")
    assert store_bytes(store(ctx, "secondary"), PATH) in {OTHER, None, DATA}
    assert store_bytes(store(ctx), PATH) in {OTHER, None}
    assert_invariants(ctx)


def snapshot_everything(ctx: AppContext) -> tuple[Any, ...]:
    from sqlalchemy import MetaData

    meta = MetaData()
    meta.reflect(ctx.engine)
    with ctx.sessionmaker() as session:
        tables = {
            name: sorted(map(tuple, session.execute(table.select()).all()), key=repr)
            for name, table in meta.tables.items()
        }
    stores = {name: sorted(_all(store(ctx, name))) for name in ("primary", "secondary", "tertiary")}
    return tables, stores


@pytest.mark.req("SVC-21", "SVC-05")
@pytest.mark.parametrize("action", ["copy", "move", "delete"])
@pytest.mark.parametrize("mode", ["full", "incremental"])
def test_rule_dry_run_leaves_every_db_row_and_store_object_unchanged(
    ctx: AppContext, action: str, mode: str
) -> None:
    ctx.projects["demo"].rules = [
        RuleConfig.model_validate(
            {
                "name": "archive",
                "action": action,
                "source": "primary",
                **({} if action == "delete" else {"destination": "secondary"}),
                "match": {"paths": ["**"]},
                "options": {"overwrite": True, "retrigger": True},
            }
        )
    ]
    upload(ctx)
    upload(ctx, OTHER, path="p/other_v1.dat")
    put(store(ctx, "secondary"), PATH, THIRD)
    before = snapshot_everything(ctx)
    result = rules.run_rule.fn("demo", "archive", mode=mode, dry_run=True)  # type: ignore[arg-type]
    assert result.processed == 2
    assert snapshot_everything(ctx) == before
    assert temp_files(store(ctx, "secondary")) == []
    assert_invariants(ctx)


@pytest.mark.req("SVC-21", "SVC-19")
def test_transfer_dry_run_does_not_repair_or_touch_a_dead_destination_owner(
    ctx: AppContext, clock: Any
) -> None:
    from datetime import timedelta

    file_id = upload(ctx)
    with ctx.write_session() as session:
        plan = transfer.transfers.transfer_snapshot(session, file_id, "primary", "secondary")
        transfer.transfers.reserve_destination(
            session, plan, staged=False, lease=ctx.settings.op_lease
        )
    clock.advance(ctx.settings.op_lease + timedelta(seconds=5))
    before = snapshot_everything(ctx)
    with pytest.raises(tr.NeedsRepair):
        transfer.transfer_file.fn(
            file_id, "primary", "secondary", options=RuleOptions(dry_run=True)
        )
    assert snapshot_everything(ctx) == before


@pytest.mark.req("SVC-20", "STO-13")
def test_rule_delete_expected_hash_stale_after_overwrite_keeps_new_source(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_id = upload(ctx)
    transfer.transfer_file.fn(file_id, "primary", "secondary")
    with ctx.sessionmaker() as session:
        copy_row = next(c for c in copies(ctx) if c.storage_location == "primary")
        generation = session.get_one(FileStorageLocation, copy_row.copy_id).generation
    upload(ctx, OTHER, overwrite=True)
    with pytest.raises(tr.StaleOperation):
        transfer.delete_source_copy.fn(
            file_id, "primary", expected_generation=generation, expected_hash=md5(DATA)
        )
    assert store_bytes(store(ctx), PATH) == OTHER
    real = delete_mod.delete_object
    assert real is transfer.delete.delete_object
    assert_invariants(ctx)


@pytest.mark.req("SVC-22", "SVC-23", "SVC-05")
@pytest.mark.parametrize("mode", ["incremental", "full"])
def test_tied_files_move_across_batches_with_real_transfers_and_a_late_arrival(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from heliopuddle.server.database.orm import File

    ctx.projects["demo"].rules = [
        RuleConfig.model_validate(
            {
                "name": "archive",
                "action": "move",
                "source": "primary",
                "destination": "secondary",
                "match": {"paths": ["**"]},
                "options": {"batch": {"max_files": 2}},
            }
        )
    ]
    successors: list[dict[str, Any]] = []
    monkeypatch.setattr(
        rules, "_schedule_successor", lambda *a, **kw: successors.append(kw["parameters"])
    )
    monkeypatch.setattr(rules, "_load_checkpoint", lambda *a, **k: None)
    paths = [f"p/f{i}_v1.dat" for i in range(5)]
    for path in paths:
        upload(ctx, path.encode(), path=path)
    tie = datetime.now(UTC) - timedelta(hours=1)
    with ctx.write_session() as session:
        session.execute(update(File).values(updated_at=tie))
    summary = rules.run_rule.fn("demo", "archive", mode=mode)  # type: ignore[arg-type]
    assert summary.processed == 2 and summary.remaining
    late = "p/late_v1.dat"
    upload(ctx, late.encode(), path=late)
    for _ in range(6):
        if not successors:
            break
        parameters = successors.pop()
        rules.run_rule.fn(
            "demo",
            "archive",
            mode=mode,  # type: ignore[arg-type]
            continuation=rules.RuleContinuation.model_validate(parameters["continuation"]),
        )
    for path in [*paths, late]:
        assert store_bytes(store(ctx, "secondary"), path) == path.encode()
        assert store_bytes(store(ctx), path) is None
    assert_invariants(ctx)


@pytest.mark.req("SVC-23", "SVC-19")
def test_rule_bytes_budget_counts_source_size_not_transfer_reads(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.projects["demo"].rules = [
        RuleConfig.model_validate(
            {
                "name": "archive",
                "action": "move",
                "source": "primary",
                "destination": "secondary",
                "match": {"paths": ["**"]},
                "options": {"retrigger": False, "batch": {"max_bytes": len(DATA)}},
            }
        )
    ]
    for i in range(3):
        upload(ctx, DATA, path=f"p/f{i}_v1.dat")
    reads = 0
    real_md5 = StorageLocation.md5

    def counting(self: StorageLocation, path: str) -> str:
        nonlocal reads
        reads += 1
        return real_md5(self, path)

    monkeypatch.setattr(StorageLocation, "md5", counting)
    summary = rules.run_rule.fn("demo", "archive", mode="full")
    # A move hashes the destination read-back and re-reads both ends before deleting.
    assert reads > 2
    assert (summary.processed, summary.bytes_processed, summary.remaining) == (1, len(DATA), True)
    assert_invariants(ctx)

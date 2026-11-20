"""Index flow: adoption, skips, batching and removal repair."""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update

from heliopuddle.config.models import DEFAULT_VERSION_RULES, VersionRule
from heliopuddle.server import models
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import FileStorageLocation, utcnow
from heliopuddle.server.models import index as index_db
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services import index as index_mod
from heliopuddle.server.services.index import (
    IndexEntry,
    index_batch,
    index_full,
)
from heliopuddle.server.services.index_names import SYSTEM_USER
from heliopuddle.storage import StorageLocation
from tests.unit._index_helpers import (
    OLD,
    _md5,
    by_path,
    copy_state,
    put,
    register,
    set_index,
    stores,
)

_FIXTURES = (stores,)  # referenced so ruff sees the imports used

pytestmark = pytest.mark.usefixtures("inline_prefect")


def age_copies(ctx: AppContext, age: timedelta) -> None:
    """Pretend every copy last changed ``age`` ago (for stale-copy repair)."""
    with ctx.write_session() as session:
        session.execute(update(FileStorageLocation).values(updated_at=utcnow() - age))


# --- adoption, skips and batching -------------------------------------------------------


@pytest.mark.req("SVC-03")
def test_adopting_a_versioned_tree_sets_series_and_latest(
    ctx: AppContext, stores: dict[str, Path]
) -> None:
    set_index(ctx, batch_size=3)
    ctx.projects["demo"].versioning = [
        VersionRule(
            paths=["cal/**"],
            version_type="text",
            extraction_pattern=r"_v(?P<major>\d+)\.(?P<minor>\d+)\.json$",
            version_text="{major:0>3}.{minor:0>4}",
        ),
        *DEFAULT_VERSION_RULES,
    ]
    root = stores["primary"]
    paths = [
        "sci/a_v1.cdf",
        "sci/a_v2.cdf",
        "sci/a_v10.cdf",
        "cal/b_v1.2.json",
        "cal/b_v1.10.json",
        "cal/b_v2.0.json",
        "notes/readme.txt",
    ]
    for p in paths:
        put(root, p, p.encode())

    summary = index_full.fn("demo", "primary")

    assert (summary.listed, summary.added) == (7, 7)
    latest = {p for p in paths if by_path(ctx, p).is_latest}
    assert latest == {"sci/a_v10.cdf", "cal/b_v2.0.json", "notes/readme.txt"}
    assert by_path(ctx, "sci/a_v2.cdf").version_number == 2
    assert by_path(ctx, "cal/b_v1.10.json").version_text == "001.0010"
    with ctx.sessionmaker() as session:
        progress = index_db.get_index_progress(session, "demo", "primary")
        assert progress is not None
        assert progress.last_full_at == summary.started_at
        assert progress.last_incremental_at is None


@pytest.mark.req("SVC-03")
def test_temp_and_settling_files_are_skipped(ctx: AppContext, stores: dict[str, Path]) -> None:
    root = stores["primary"]
    put(root, "s/written.dat.hp-tmp", b"partial")
    put(root, "s/fresh.dat", b"fresh", age=timedelta(seconds=5))
    put(root, "s/old.dat", b"old")

    summary = index_full.fn("demo", "primary")

    assert (summary.listed, summary.added, summary.skipped) == (3, 1, 2)
    with ctx.sessionmaker() as session:
        assert models.get_file_by_path(session, "demo", "s/fresh.dat") is None
        assert models.get_file_by_path(session, "demo", "s/written.dat.hp-tmp") is None

    set_index(ctx, settle_time=timedelta(seconds=1))
    assert index_full.fn("demo", "primary").added == 1
    assert by_path(ctx, "s/fresh.dat").is_latest


@pytest.mark.req("SVC-03")
def test_young_pending_copy_is_skipped(ctx: AppContext, stores: dict[str, Path]) -> None:
    put(stores["primary"], "p/a.dat", b"a")
    put(stores["primary"], "p/b.dat", b"b")
    register(ctx, "primary", "p/a.dat", b"a")
    register(ctx, "primary", "p/b.dat", b"b")
    with ctx.write_session() as session:
        # p/a.dat: an upload owns it; p/b.dat: observed, awaiting verification.
        session.execute(
            update(FileStorageLocation)
            .where(FileStorageLocation.file_id == by_path(ctx, "p/a.dat").id)
            .values(op_token=str(uuid.uuid4()), op_deadline=utcnow() + timedelta(hours=1))
        )
        session.execute(
            update(FileStorageLocation)
            .where(FileStorageLocation.file_id == by_path(ctx, "p/b.dat").id)
            .values(mtime=utcnow(), last_seen_at=utcnow())
        )

    summary = index_full.fn("demo", "primary")

    assert (summary.skipped, summary.reverified, summary.added) == (2, 0, 0)
    assert copy_state(by_path(ctx, "p/a.dat"), "primary") == FileState.PENDING
    assert copy_state(by_path(ctx, "p/b.dat"), "primary") == FileState.PENDING


@pytest.mark.req("SVC-03")
def test_invalid_and_vanished_paths_are_skipped(
    ctx: AppContext, stores: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    put(stores["primary"], "v/gone.dat", b"x")
    real = StorageLocation.md5

    def vanished(self: StorageLocation, path: str) -> str:
        if path == "v/gone.dat":
            raise FileNotFoundError(path)
        return real(self, path)  # pragma: no cover

    monkeypatch.setattr(StorageLocation, "md5", vanished)
    counts = index_batch.fn(
        "demo",
        "primary",
        [
            IndexEntry(path="v/gone.dat", size=1),
            IndexEntry(path="v/../bad.dat", size=1, md5=_md5(b"")),
        ],
    )
    assert (counts.skipped, counts.added) == (2, 0)


@pytest.mark.req("SVC-03")
def test_index_batch_uses_native_md5(ctx: AppContext, stores: dict[str, Path]) -> None:
    put(stores["primary"], "n/a.dat", b"content")
    entry = IndexEntry(path="n/a.dat", size=7, md5="ABCDEF0123456789ABCDEF0123456789")
    counts = index_batch.fn("demo", "primary", [entry])
    assert counts.added == 1
    assert by_path(ctx, "n/a.dat").hash == "abcdef0123456789abcdef0123456789"


@pytest.mark.req("SVC-03")
def test_failed_batch_fails_the_flow_and_keeps_progress(
    ctx: AppContext, stores: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    put(stores["primary"], "f/bad.dat", b"bad")
    put(stores["primary"], "f/good.dat", b"good")

    def broken(self: StorageLocation, path: str) -> str:
        if path == "f/bad.dat":
            raise PermissionError(path)
        return _md5((stores["primary"] / path).read_bytes())

    monkeypatch.setattr(StorageLocation, "md5", broken)
    with pytest.raises(RuntimeError, match="failed"):
        index_full.fn("demo", "primary")
    assert by_path(ctx, "f/good.dat").is_latest
    with ctx.sessionmaker() as session:
        assert index_db.get_index_progress(session, "demo", "primary") is None


@pytest.mark.req("SVC-03")
def test_unknown_storage_location_fails(ctx: AppContext) -> None:
    with pytest.raises(LookupError):
        index_full.fn("demo", "nope")


@pytest.mark.req("SVC-03")
def test_storage_location_missing_from_db_fails(ctx: AppContext) -> None:
    with ctx.write_session() as session:
        from heliopuddle.server.database.orm import StorageLocation as Row

        session.execute(update(Row).where(Row.name == "secondary").values(name="renamed"))
    with pytest.raises(models.NotFoundError):
        index_full.fn("demo", "secondary")


# --- removals race and repair ---------------------------------------------------------


@pytest.mark.req("SVC-03")
def test_removal_rechecks_store_and_skips_files_that_exist(
    ctx: AppContext, stores: dict[str, Path]
) -> None:
    put(stores["primary"], "q/a.dat", b"a")
    index_full.fn("demo", "primary")
    with ctx.sessionmaker() as session:
        copy = session.scalars(select(FileStorageLocation)).one()
        candidate = index_mod.RemovalCandidate(
            copy_id=copy.id, generation=copy.generation, path="q/a.dat"
        )
    gone = index_mod.RemovalCandidate(copy_id=999, generation=0, path="q/none.dat")
    counts = index_mod.index_removals("demo", "primary", [candidate, gone], utcnow())
    assert (counts.skipped, counts.removed) == (2, 0)

    # A copy changed after the listing started is left alone.
    (stores["primary"] / "q/a.dat").unlink()
    counts = index_mod.index_removals("demo", "primary", [candidate], utcnow() - timedelta(days=1))
    assert (counts.skipped, counts.removed) == (1, 0)
    # So is a copy whose generation changed since it was selected.
    stale = candidate.model_copy(update={"generation": candidate.generation - 1})
    counts = index_mod.index_removals("demo", "primary", [stale], utcnow())
    assert (counts.skipped, counts.removed) == (1, 0)
    assert copy_state(by_path(ctx, "q/a.dat"), "primary") == FileState.AVAILABLE


@pytest.mark.req("SVC-10")
def test_stale_pending_copy_is_reverified(ctx: AppContext, stores: dict[str, Path]) -> None:
    put(stores["primary"], "p/ok.dat", b"ok")
    register(ctx, "primary", "p/ok.dat", b"ok")
    register(ctx, "primary", "p/lost.dat", b"lost")  # never written
    age_copies(ctx, timedelta(hours=2))

    with ctx.write_session() as session:  # observed once, so not a re-check (SVC-12)
        session.execute(
            update(FileStorageLocation).values(
                last_seen_at=utcnow() - OLD, updated_at=utcnow() - OLD
            )
        )

    summary = index_full.fn("demo", "primary")

    assert summary.reverified == 2
    assert copy_state(by_path(ctx, "p/ok.dat"), "primary") == FileState.AVAILABLE
    lost = by_path(ctx, "p/lost.dat")
    assert copy_state(lost, "primary") == FileState.UNKNOWN
    assert not lost.is_latest


@pytest.mark.req("SVC-10", "SVC-12")
def test_never_observed_pending_copy_is_rechecked_or_removed(
    ctx: AppContext, stores: dict[str, Path]
) -> None:
    put(stores["primary"], "p/ok.dat", b"ok")
    put(stores["primary"], "p/bad.dat", b"other content")
    register(ctx, "primary", "p/ok.dat", b"ok")
    register(ctx, "primary", "p/bad.dat", b"bad")
    register(ctx, "primary", "p/lost.dat", b"lost")  # never written

    summary = index_full.fn("demo", "primary")

    assert (summary.reverified, summary.unknown, summary.removed) == (1, 1, 1)
    assert copy_state(by_path(ctx, "p/ok.dat"), "primary") == FileState.AVAILABLE
    bad = by_path(ctx, "p/bad.dat")
    assert copy_state(bad, "primary") == FileState.UNKNOWN
    assert bad.hash == _md5(b"bad") and not bad.is_latest
    lost = by_path(ctx, "p/lost.dat", include_deleted=True)
    assert lost.deleted_at is not None


@pytest.mark.req("SVC-10")
def test_stale_deleting_copy_is_retried(ctx: AppContext, stores: dict[str, Path]) -> None:
    put(stores["primary"], "d/a_v1.dat", b"1")
    put(stores["primary"], "d/a_v2.dat", b"2")
    index_full.fn("demo", "primary")
    file_id = by_path(ctx, "d/a_v2.dat").id
    with ctx.write_session() as session:
        # An abandoned delete: its ownership has already expired.
        models.begin_delete(session, file_id, updated_by="alice", lease=timedelta(0))
    age_copies(ctx, timedelta(hours=2))

    summary = index_full.fn("demo", "primary")

    # The abandoned delete is finished by the expired-operation repair.
    assert summary.repaired == 1 and summary.repair_failed == 0
    assert summary.skipped == 1  # the deleting copy is not diffed
    assert not (stores["primary"] / "d/a_v2.dat").exists()
    gone = by_path(ctx, "d/a_v2.dat", include_deleted=True)
    assert gone.deleted_at is not None
    assert gone.updated_by == SYSTEM_USER
    assert by_path(ctx, "d/a_v1.dat").is_latest


@pytest.mark.req("SVC-10")
def test_failed_repair_fails_the_flow(
    ctx: AppContext, stores: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    register(ctx, "primary", "p/x.dat", b"x")
    put(stores["primary"], "d/y.dat", b"y")
    index_full.fn("demo", "primary")
    with ctx.write_session() as session:
        models.begin_delete(session, by_path(ctx, "d/y.dat").id, lease=timedelta(0))
    age_copies(ctx, timedelta(hours=2))

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise OSError("store down")

    monkeypatch.setattr(StorageLocation, "info", boom)
    monkeypatch.setattr(models, "finish_delete", boom)
    with pytest.raises(RuntimeError, match=r"repair"):
        index_full.fn("demo", "primary")

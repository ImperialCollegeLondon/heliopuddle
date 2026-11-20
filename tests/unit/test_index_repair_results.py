"""Index hardening: repair results and non-integer versions."""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import update

from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import (
    FileStorageLocation,
    IndexProgress,
    Series,
    utcnow,
)
from heliopuddle.server.models import index as index_db
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services import index_finish
from heliopuddle.server.services.index import (
    index_full,
)
from heliopuddle.storage import StorageLocation
from tests.unit._index_hardening_helpers import (
    OLD,
    _md5,
    copy_row,
    file_row,
    put,
    root,
    set_copy,
)

_FIXTURES = (root,)  # referenced so ruff sees the imports used

pytestmark = pytest.mark.usefixtures("inline_prefect")


# --- SVC-15: repair results are checked -----------------------------------------------


def _progress(ctx: AppContext) -> IndexProgress | None:
    with ctx.sessionmaker() as session:
        return index_db.get_index_progress(session, "demo", "primary")


@pytest.mark.req("SVC-15")
def test_incomplete_delete_repair_fails_the_run_and_keeps_progress(
    ctx: AppContext, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    put(root, "r/a.dat", b"a")
    index_full.fn("demo", "primary")
    progress = _progress(ctx)
    assert progress is not None
    last_full = progress.last_full_at
    set_copy(ctx, "r/a.dat", state=FileState.DELETING, updated_at=utcnow() - OLD)
    calls: list[str] = []

    def failing_rm(self: StorageLocation, path: str) -> None:
        calls.append(path)
        raise OSError("store unavailable")

    # The repair really runs and the store refuses the delete: it is incomplete.
    monkeypatch.setattr(StorageLocation, "rm", failing_rm)
    with pytest.raises(RuntimeError, match="repair"):
        index_full.fn("demo", "primary")

    assert calls == ["r/a.dat"]
    progress = _progress(ctx)
    assert progress is not None and progress.last_full_at == last_full
    assert (root / "r/a.dat").exists()
    assert copy_row(ctx, "r/a.dat").state == FileState.DELETING


@pytest.mark.req("SVC-15")
def test_crashed_verify_repair_fails_the_run(
    ctx: AppContext, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    put(root, "r/b.dat", b"b")
    index_full.fn("demo", "primary")
    set_copy(
        ctx,
        "r/b.dat",
        state=FileState.PENDING,
        updated_at=utcnow() - OLD,
        last_seen_at=utcnow() - OLD,
    )
    monkeypatch.setattr(index_finish, "verify_file", lambda copy_id, **kw: _Failed())

    with pytest.raises(RuntimeError, match="repair"):
        index_full.fn("demo", "primary")

    assert copy_row(ctx, "r/b.dat").state == FileState.PENDING
    progress = _progress(ctx)
    assert progress is not None and progress.last_full_at is not None


@pytest.mark.req("SVC-15")
def test_dropped_verify_result_counts_only_while_still_pending(
    ctx: AppContext, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    put(root, "r/c.dat", b"c")
    index_full.fn("demo", "primary")
    set_copy(
        ctx,
        "r/c.dat",
        state=FileState.PENDING,
        updated_at=utcnow() - OLD,
        last_seen_at=utcnow() - OLD,
    )

    def stale_then_changed(copy_id: int, **kwargs: Any) -> Any:
        # A guarded verify dropped its stale result; someone else then made
        # the copy available, so nothing is left to repair.
        with ctx.write_session() as session:
            session.execute(
                update(FileStorageLocation)
                .where(FileStorageLocation.id == copy_id)
                .values(state=FileState.AVAILABLE)
            )
        return _Completed(FileState.PENDING)

    monkeypatch.setattr(index_finish, "verify_file", stale_then_changed)
    summary = index_full.fn("demo", "primary")

    assert (summary.reverified, summary.repair_failed) == (1, 0)


@pytest.mark.req("SVC-15")
@pytest.mark.parametrize(
    ("status", "repaired", "failed"),
    [
        ("repaired", 1, 0),
        ("dropped", 1, 0),
        ("not_owned_or_live", 0, 0),
        ("incomplete", 0, 1),
        ("raises", 0, 1),
    ],
)
def test_expired_operations_are_repaired_and_results_checked(
    ctx: AppContext,
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    repaired: int,
    failed: int,
) -> None:
    put(root, "x/a.dat", b"a")
    index_full.fn("demo", "primary")
    copy = copy_row(ctx, "x/a.dat")
    set_copy(ctx, "x/a.dat", op_token=str(uuid.uuid4()), op_deadline=utcnow() - OLD)
    seen: list[tuple[int, int, datetime]] = []

    def find(ctx_: AppContext, storage_location_id: int, now: datetime) -> list[int]:
        seen.append((storage_location_id, copy.id, now))
        return [copy.id]

    def repair(copy_id: int, now: datetime) -> str:
        assert copy_id == copy.id
        if status == "raises":
            raise OSError("store down")
        if status in ("repaired", "dropped"):
            set_copy(ctx, "x/a.dat", op_token=None, op_deadline=None)
        return status

    monkeypatch.setattr(index_finish, "_find_expired", find)
    monkeypatch.setattr(index_finish, "_repair_expired", repair)
    if failed:
        with pytest.raises(RuntimeError, match="repair"):
            index_full.fn("demo", "primary")
    else:
        summary = index_full.fn("demo", "primary")
        assert (summary.repaired, summary.repair_failed) == (repaired, failed)
    assert seen and seen[0][0] == copy.storage_location_id
    assert file_row(ctx, "x/a.dat").deleted_at is None  # never taken for removed


@pytest.mark.req("SVC-15")
def test_repair_adapters_call_the_operation_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    calls: list[Any] = []
    uploads = types.ModuleType("heliopuddle.server.models.uploads")
    uploads.find_expired = lambda session, sl, now: calls.append(("find", sl)) or [3, 4]  # type: ignore[attr-defined]
    upload = types.ModuleType("heliopuddle.server.services.upload")

    class Status:
        value = "dropped"

    class Result:
        status = Status()

    upload.repair_expired = lambda copy_id, now=None: calls.append(("repair", copy_id)) or Result()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "heliopuddle.server.models.uploads", uploads)
    monkeypatch.setitem(sys.modules, "heliopuddle.server.services.upload", upload)

    class Ctx:
        def sessionmaker(self) -> Any:
            import contextlib

            return contextlib.nullcontext(None)

    now = utcnow()
    found = index_finish._find_expired(Ctx(), 7, now)  # type: ignore[arg-type]  # pyright: ignore[reportPrivateUsage]
    status = index_finish._repair_expired(3, now)  # pyright: ignore[reportPrivateUsage]
    assert (found, status) == ([3, 4], "dropped")
    assert calls == [("find", 7), ("repair", 3)]


class _Completed:
    def __init__(self, value: Any) -> None:
        self.value = value
        self.message = None

    def is_completed(self) -> bool:
        return True

    def result(self, raise_on_failure: bool = True) -> Any:
        return self.value


class _Failed:
    message = "boom"

    def is_completed(self) -> bool:
        return False

    def result(self, raise_on_failure: bool = True) -> Any:  # pragma: no cover
        raise RuntimeError("boom")


# --- DEP-12: a version group that is not an integer ------------------------------------


@pytest.mark.req("DEP-12")
def test_non_integer_version_is_recorded_unknown_not_crashing(ctx: AppContext, root: Path) -> None:
    from heliopuddle.config.models import VersionRule

    ctx.projects["demo"].versioning = [
        VersionRule(paths=["**"], extraction_pattern=r"_(?P<version>[a-z0-9]+)\.dat$")
    ]
    put(root, "v/x_v1.dat", b"letters")
    put(root, "v/y_12.dat", b"digits")

    summary = index_full.fn("demo", "primary")

    assert (summary.added, summary.unknown) == (2, 1)
    odd = file_row(ctx, "v/x_v1.dat")
    assert copy_row(ctx, "v/x_v1.dat").state == FileState.UNKNOWN
    assert odd.hash == _md5(b"letters") and not odd.is_latest
    ok = file_row(ctx, "v/y_12.dat")
    assert (ok.version_number, ok.is_latest) == (12, True)
    with ctx.sessionmaker() as session:
        assert session.get_one(Series, odd.series_id).series_key == "p:v/x_v1.dat"


@pytest.mark.req("API-21")
def test_version_of_the_other_kind_is_recorded_unknown_in_its_literal_series(
    ctx: AppContext, root: Path
) -> None:
    from heliopuddle.config.models import VersionRule

    # A number series whose rule later becomes a text rule.
    put(root, "k/a_v1.dat", b"one")
    assert index_full.fn("demo", "primary").added == 1
    ctx.projects["demo"].versioning = [
        VersionRule(
            paths=["**"], version_type="text", extraction_pattern=r"_v(?P<version>\d+)\.dat$"
        )
    ]
    put(root, "k/a_v2.dat", b"two")

    summary = index_full.fn("demo", "primary")

    assert (summary.added, summary.unknown) == (1, 1)
    odd = file_row(ctx, "k/a_v2.dat")
    assert copy_row(ctx, "k/a_v2.dat").state == FileState.UNKNOWN
    assert (odd.version_number, odd.version_text, odd.is_latest) == (None, None, False)
    first = file_row(ctx, "k/a_v1.dat")
    assert (first.version_number, first.is_latest) == (1, True)
    with ctx.sessionmaker() as session:
        assert session.get_one(Series, odd.series_id).series_key == "p:k/a_v2.dat"
        assert session.get_one(Series, first.series_id).version_type == "number"

"""Series: derivation, latest tracking and metadata."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from heliopuddle.config.models import VersionRule
from heliopuddle.server import models
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import File, Series
from heliopuddle.server.schemas import FileState
from heliopuddle.versioning import derive, display_series_key
from tests.unit._series_helpers import (
    RULES,
    _stored,
    latest_paths,
    reg,
    remove,
)


@pytest.mark.req("API-02")
@pytest.mark.parametrize(
    ("path", "series_key", "number", "text", "kind"),
    [
        ("sci/a_20250101_v002.cdf", "sci/a_20250101_v{v}.cdf", 2, None, "number"),
        # The default rule does not take a major.minor pair or a date.
        ("sci/a_20250101_v002.0003.cdf", "sci/a_20250101_v002.0003.cdf", None, None, None),
        ("sci/a_20250101.cdf", "sci/a_20250101.cdf", None, None, None),
        (
            "calibration/layers/x_v1.4.json",
            "calibration/layers/x_v{major}.{minor}.json",
            None,
            "001.0004",
            "text",
        ),
        ("spice/k_07.bsp", "spice/k_{v}.bsp", 7, None, "number"),
        ("notes/readme.txt", "notes/readme.txt", None, None, None),
    ],
)
def test_register_derives_series_and_version(
    ctx: AppContext,
    path: str,
    series_key: str,
    number: int | None,
    text: str | None,
    kind: str | None,
) -> None:
    result = reg(ctx, path)
    with ctx.sessionmaker() as session:
        f = models.get_file(session, result.file_id)
        assert f is not None
        record = models.to_file_read(f)
        assert f.series.version_type == kind
    assert record.series_key == series_key
    assert (record.version_number, record.version_text) == (number, text)
    assert record.is_latest
    assert latest_paths(ctx, series_key) == ([path], path)


@pytest.mark.req("API-02")
def test_add_and_remove_sequence_keeps_one_latest(ctx: AppContext) -> None:
    key = "s/x_v{v}.dat"
    v1 = reg(ctx, "s/x_v001.dat").file_id
    assert latest_paths(ctx, key) == (["s/x_v001.dat"], "s/x_v001.dat")
    v3 = reg(ctx, "s/x_v3.dat").file_id
    v2 = reg(ctx, "s/x_v02.dat").file_id  # an older version added later is not latest
    assert latest_paths(ctx, key) == (["s/x_v3.dat"], "s/x_v3.dat")

    remove(ctx, v3)
    assert latest_paths(ctx, key) == (["s/x_v02.dat"], "s/x_v02.dat")
    remove(ctx, v1)
    assert latest_paths(ctx, key) == (["s/x_v02.dat"], "s/x_v02.dat")
    remove(ctx, v2)
    assert latest_paths(ctx, key) == ([], None)

    # Re-adding a deleted path creates a new row; the old rows stay as history.
    again = reg(ctx, "s/x_v3.dat").file_id
    assert again != v3
    assert latest_paths(ctx, key) == (["s/x_v3.dat"], "s/x_v3.dat")
    with ctx.sessionmaker() as session:
        rows = session.scalars(select(File).where(File.path == "s/x_v3.dat")).all()
        assert sorted(r.deleted_at is None for r in rows) == [False, True]
        old = session.get_one(File, v3)
        assert old.updated_by == "carol"
        assert not old.is_latest


@pytest.mark.req("API-02")
@pytest.mark.parametrize(
    ("paths", "latest"),
    [
        # number: 10 beats 9; leading zeros and digit counts do not matter
        (["z/b_v10.cdf", "z/b_v009.cdf", "z/b_v00011.cdf"], "z/b_v00011.cdf"),
        # text, compared as strings: "002.0000" beats "001.0010" beats "001.0009"
        (
            [
                "calibration/layers/x_v1.9.json",
                "calibration/layers/x_v2.0.json",
                "calibration/layers/x_v1.10.json",
            ],
            "calibration/layers/x_v2.0.json",
        ),
        # zero-padded text: "001.0010" beats "001.0002"
        (
            ["calibration/layers/y_v1.10.json", "calibration/layers/y_v1.2.json"],
            "calibration/layers/y_v1.10.json",
        ),
        # equal versions: the newest file wins
        (["t/c_v1.cdf", "t/c_v01.cdf", "t/c_v001.cdf"], "t/c_v001.cdf"),
    ],
)
def test_latest_ordering(ctx: AppContext, paths: list[str], latest: str) -> None:
    for p in paths:
        reg(ctx, p)
    key = display_series_key(derive(paths[0], RULES).series_key)
    assert latest_paths(ctx, key) == ([latest], latest)
    with ctx.sessionmaker() as session:
        series = session.scalars(select(Series).where(Series.series_key.in_(_stored(key)))).one()
        _, project, files = models.list_versions(session, series.id)
    assert project == "demo"
    assert files[0].path == latest


@pytest.mark.req("API-02")
def test_other_extensions_and_unversioned_paths_are_separate_series(
    ctx: AppContext,
) -> None:
    a = reg(ctx, "d/a_v2.cdf").file_id
    b = reg(ctx, "d/a_v2.csv").file_id
    c = reg(ctx, "d/readme.txt").file_id
    d = reg(ctx, "d/other.txt").file_id
    with ctx.sessionmaker() as session:
        series = {session.get_one(File, i).series_id for i in (a, b, c, d)}
        assert len(series) == 4
        assert all(session.get_one(File, i).is_latest for i in (a, b, c, d))


@pytest.mark.req("API-02")
def test_content_date_from_rule_or_request(ctx: AppContext) -> None:
    given = datetime(2024, 5, 6, 7, tzinfo=UTC)
    cases = {
        "dated/a_20250102_v1.cdf": (None, datetime(2025, 1, 2, tzinfo=UTC)),
        "dated/b_20250102_v1.cdf": (given, given),
        "dated/c_20251399_v1.cdf": (None, None),  # unparseable date is ignored
        "sci/d_v1.cdf": (None, None),
    }
    for path, (request_date, expected) in cases.items():
        file_id = reg(ctx, path, content_date=request_date).file_id
        with ctx.sessionmaker() as session:
            assert session.get_one(File, file_id).content_date == expected, path


@pytest.mark.req("API-02", "SVC-05")
def test_recompute_latest_updates_timestamp_when_flag_changes(ctx: AppContext) -> None:
    v1 = reg(ctx, "u/a_v1.cdf").file_id
    with ctx.sessionmaker() as session:
        before = session.get_one(File, v1).updated_at
    reg(ctx, "u/a_v2.cdf")
    with ctx.sessionmaker() as session:
        f = session.get_one(File, v1)
        assert not f.is_latest
        assert f.updated_at > before


@pytest.mark.req("API-02")
def test_rule_change_keeps_existing_series(ctx: AppContext) -> None:
    first = reg(ctx, "r/x1_v1.cdf", rules=[])  # no rules: unversioned
    # A later rule gives the same path another series key; the file keeps its series.
    other = [VersionRule(paths=["r/**"], extraction_pattern=r"x(?P<version>\d)_")]
    assert derive("r/x1_v1.cdf", other).series_key == "v:r/x{v}_v1.cdf"
    again = reg(ctx, "r/x1_v1.cdf", rules=other)
    assert again.file_id == first.file_id
    assert again.outcome == models.RegisterOutcome.UNCHANGED
    with ctx.sessionmaker() as session:
        f = models.get_file(session, first.file_id)
        assert f is not None and f.series.series_key == "p:r/x1_v1.cdf"
        assert f.is_latest


@pytest.mark.req("API-07")
def test_same_hash_reregister_reverifies_only_when_not_available(ctx: AppContext) -> None:
    first = reg(ctx, "a/f.dat", "abc")
    assert first.needs_verify
    with ctx.write_session() as session:
        models.set_copy_verified(session, first.copy_id, size=1, mtime=None, hash="abc")
    same = reg(ctx, "a/f.dat", "ABC", created_by="bob", metadata={"ignored": True})
    assert (same.file_id, same.copy_id) == (first.file_id, first.copy_id)
    assert same.outcome == models.RegisterOutcome.UNCHANGED
    assert not same.needs_verify
    with ctx.write_session() as session:
        models.set_copy_state(session, first.copy_id, FileState.UNKNOWN)
    again = reg(ctx, "a/f.dat", "abc")
    assert again.needs_verify
    # A same-hash register into another storage location adds a pending copy there.
    other = reg(ctx, "a/f.dat", "abc", location="secondary")
    assert other.copy_id != first.copy_id
    assert other.needs_verify
    with ctx.sessionmaker() as session:
        f = models.get_file(session, first.file_id)
        assert f is not None
        assert [c.state for c in f.copies] == [FileState.PENDING, FileState.PENDING]
        assert f.updated_by is None
        assert f.file_metadata is None


@pytest.mark.req("API-02")
def test_lock_series_resolves_a_concurrent_insert(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    reg(ctx, "c/k_v1.cdf")
    info = derive("c/k_v2.cdf", RULES)
    with ctx.write_session() as session:
        project_id = models.get_project(session, "demo").id  # type: ignore[union-attr]
        real_scalar = Session.scalar
        calls = {"n": 0}

        def first_misses(self: Session, *args: Any, **kwargs: Any) -> Any:
            calls["n"] += 1
            return None if calls["n"] == 1 else real_scalar(self, *args, **kwargs)

        monkeypatch.setattr(Session, "scalar", first_misses)
        series = models.lock_series(session, project_id, info)
        monkeypatch.undo()
        assert series.series_key == "v:c/k_v{v}.cdf"
        assert session.scalar(select(func.count(Series.id))) == 1


@pytest.mark.req("SVC-07")
def test_delete_model_steps(ctx: AppContext) -> None:
    file_id = reg(ctx, "x/a_v1.dat").file_id
    reg(ctx, "x/a_v1.dat", location="secondary")
    with ctx.write_session() as session:
        project, copies = models.begin_delete(session, file_id, updated_by="u")
    assert project == "demo"
    assert [c.storage_location for c in copies] == ["primary", "secondary"]
    # Only one copy was removed from its store: the file stays live, and the
    # other copy stays deleting without a token (a delete that did not finish).
    with ctx.write_session() as session:
        f = models.finish_delete(
            session, file_id, copies, {copies[1].copy_id: FileState.DELETING}, updated_by="u"
        )
        assert f.deleted_at is None
        assert [c.state for c in f.copies] == [FileState.DELETED, FileState.DELETING]
        assert [c.op_token for c in f.copies] == [None, None]
    # A copy still owned by a live delete is busy.
    with ctx.write_session() as session:
        _, owned = models.begin_delete(session, file_id)
    with pytest.raises(models.CopyBusy), ctx.write_session() as session:
        models.begin_delete(session, file_id)
    with ctx.write_session() as session:
        models.finish_delete(session, file_id, owned, {owned[0].copy_id: FileState.DELETING})
    # A rerun only has the remaining copy to delete.
    with ctx.write_session() as session:
        _, copies = models.begin_delete(session, file_id)
    assert [c.storage_location for c in copies] == ["secondary"]
    with ctx.write_session() as session:
        f = models.finish_delete(session, file_id, [copies[0]], updated_by="v")
        assert f.deleted_at is not None
        assert f.updated_by == "v"
        assert not f.is_latest
    with ctx.write_session() as session:
        assert models.finish_delete(session, file_id, []).deleted_at is not None
        with pytest.raises(models.NotFoundError):
            models.begin_delete(session, file_id)
        with pytest.raises(models.NotFoundError):
            models.begin_delete(session, 999)
        with pytest.raises(models.NotFoundError):
            models.finish_delete(session, 999, [])


@pytest.mark.req("API-08")
def test_update_metadata_merge_replace_and_errors(ctx: AppContext) -> None:
    file_id = reg(ctx, "m/a.dat", metadata={"a": 1, "b": 2}).file_id
    with ctx.write_session() as session:
        f = models.update_metadata(session, file_id, {"b": None, "c": 3}, updated_by="u")
        assert f.file_metadata == {"a": 1, "c": 3}
        assert f.updated_by == "u"
    with ctx.write_session() as session:
        f = models.update_metadata(session, file_id, {"z": 0, "y": None}, replace=True)
        assert f.file_metadata == {"z": 0}
        assert f.updated_by is None
    remove(ctx, file_id)
    with ctx.write_session() as session:
        with pytest.raises(models.NotFoundError):
            models.update_metadata(session, file_id, {})
        with pytest.raises(models.NotFoundError):
            models.update_metadata(session, 999, {})
        with pytest.raises(models.NotFoundError):
            models.list_versions(session, 999)

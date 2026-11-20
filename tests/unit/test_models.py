"""DB access functions: config sync (DB-03) and file registration (API-01)."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from heliopuddle.config import load_projects
from heliopuddle.server import models
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import File, Project, Series, StorageLocation
from heliopuddle.server.schemas import FileState


def _locations(ctx: AppContext) -> dict[str, tuple[str, bool, bool]]:
    with ctx.sessionmaker() as session:
        rows = session.scalars(select(StorageLocation).order_by(StorageLocation.id)).all()
        return {r.name: (r.url, r.accept_external_changes, r.active) for r in rows}


@pytest.mark.req("DB-03")
def test_sync_config_upserts_deactivates_and_reactivates(
    ctx: AppContext, config_dir: Path, tmp_path: Path
) -> None:
    before = _locations(ctx)
    assert set(before) == {"primary", "secondary"}
    assert all(active for _, _, active in before.values())
    ids_before = _ids(ctx)

    # Second start: secondary removed, primary changed, a new project added.
    (config_dir / "demo.yaml").write_text(
        "project: demo\nstorage_locations:\n"
        "  - {name: primary, url: 'file:///moved', accept_external_changes: false}\n"
    )
    (config_dir / "other.yaml").write_text(
        "project: other\nstorage_locations:\n  - {name: s, url: 'file:///o'}\n"
    )
    ctx.projects = load_projects(config_dir)
    ctx.sync_config()

    after = _locations(ctx)
    assert after["primary"] == ("file:///moved", False, True)
    assert after["secondary"][2] is False  # kept, inactive
    assert after["s"] == ("file:///o", True, True)
    assert {k: v for k, v in _ids(ctx).items() if k in ids_before} == ids_before

    # Third start: secondary returns.
    (config_dir / "demo.yaml").write_text(
        "project: demo\nstorage_locations:\n"
        "  - {name: primary, url: 'file:///moved'}\n"
        "  - {name: secondary, url: 'file:///back'}\n"
    )
    ctx.projects = load_projects(config_dir)
    ctx.sync_config()
    after = _locations(ctx)
    assert after["secondary"] == ("file:///back", True, True)
    assert _ids(ctx)["secondary"] == ids_before["secondary"]
    with ctx.sessionmaker() as session:
        assert sorted(session.scalars(select(Project.name))) == ["demo", "other"]


def _ids(ctx: AppContext) -> dict[str, int]:
    with ctx.sessionmaker() as session:
        return {r.name: r.id for r in session.scalars(select(StorageLocation))}


@pytest.mark.req("API-01")
@pytest.mark.parametrize(
    "path",
    ["", "/abs", "a/../b", "..", "a/./b", "a//b", "a/", "a\\b", "x.hp-tmp", "d/f.fits.hp-tmp"],
)
def test_validate_path_rejects(path: str) -> None:
    with pytest.raises(ValueError):
        models.validate_path(path)


@pytest.mark.req("API-01")
def test_validate_path_accepts() -> None:
    assert models.validate_path("a/b/c.dat") == "a/b/c.dat"
    assert models.validate_path("file.hp-tmp.txt") == "file.hp-tmp.txt"


@pytest.mark.req("API-01")
def test_register_file_creates_series_file_and_pending_copy(ctx: AppContext) -> None:
    with ctx.write_session() as session:
        result = models.register_file(
            session,
            project="demo",
            storage_location="primary",
            path="d/a.dat",
            hash="ABC",
            size=3,
            metadata={"k": 1},
            created_by="alice",
        )
        copy_id, file_id = result.copy_id, result.file_id
        assert result.outcome == models.RegisterOutcome.CREATED
        assert result.needs_verify
    with ctx.sessionmaker() as session:
        f = models.get_file(session, file_id)
        assert f is not None
        record = models.to_file_read(f)
    assert record.project == "demo"
    assert record.path == "d/a.dat"
    assert record.name == "a.dat"
    assert record.hash == "abc"
    assert record.hash_algo == "md5"
    assert record.metadata == {"k": 1}
    assert record.created_by == "alice"
    assert record.is_latest
    assert record.state == FileState.PENDING
    assert record.storage_locations[0].storage_location == "primary"
    with ctx.sessionmaker() as session:
        series = session.get_one(Series, record.series_id)
        assert series.series_key == "p:d/a.dat"
        assert series.latest_file_id == file_id
        copy = models.get_copy(session, copy_id)
        assert copy is not None and copy.file.path == "d/a.dat"
    assert record.series_key == "d/a.dat"
    assert record.updated_by is None


@pytest.mark.req("API-07")
def test_register_different_hash_is_a_conflict(ctx: AppContext) -> None:
    # A register never overwrites; overwrites use the upload protocol (STO-09).
    with ctx.write_session() as session:
        first = models.register_file(
            session, project="demo", storage_location="primary", path="a", hash="h1", size=1
        )
        file_id = first.file_id
    with ctx.write_session() as session, pytest.raises(models.ConflictError) as err:
        models.register_file(
            session, project="demo", storage_location="primary", path="a", hash="h2", size=2
        )
    assert err.value.file_id == file_id
    with ctx.sessionmaker() as session:
        f = session.get_one(File, file_id)
        assert (f.hash, f.size) == ("h1", 1)
        assert len(session.scalars(select(Series)).all()) == 1


@pytest.mark.req("API-01")
def test_register_errors(ctx: AppContext, config_dir: Path) -> None:
    with ctx.write_session() as session:
        with pytest.raises(models.NotFoundError, match="project"):
            models.register_file(
                session, project="nope", storage_location="primary", path="a", hash="h", size=1
            )
        with pytest.raises(models.NotFoundError, match="storage location"):
            models.register_file(
                session, project="demo", storage_location="nope", path="a", hash="h", size=1
            )
        with pytest.raises(ValueError, match="relative"):
            models.register_file(
                session, project="demo", storage_location="primary", path="/a", hash="h", size=1
            )
    (config_dir / "demo.yaml").write_text(
        "project: demo\nstorage_locations:\n  - {name: primary, url: 'file:///x'}\n"
    )
    ctx.projects = load_projects(config_dir)
    ctx.sync_config()
    with ctx.write_session() as session, pytest.raises(models.InactiveError):
        models.register_file(
            session, project="demo", storage_location="secondary", path="a", hash="h", size=1
        )


@pytest.mark.req("API-01")
def test_list_files_newest_first_with_total(ctx: AppContext) -> None:
    for i in range(5):
        with ctx.write_session() as session:
            models.register_file(
                session,
                project="demo",
                storage_location="primary",
                path=f"f{i}",
                hash="h",
                size=i,
            )
    with ctx.sessionmaker() as session:
        items, total = models.list_files(session, "demo", limit=2, offset=1)
        assert total == 5
        assert [f.path for f in items] == ["f3", "f2"]
        items, total = models.list_files(session, "other", limit=10, offset=0)
        assert (items, total) == ([], 0)
        assert models.get_file(session, 999) is None
        assert models.get_copy(session, 999) is None


@pytest.mark.req("API-03")
def test_storage_location_queries(ctx: AppContext) -> None:
    with ctx.sessionmaker() as session:
        names = [s.name for s in models.list_storage_locations(session, "demo")]
        assert names == ["primary", "secondary"]
        loc = models.get_storage_location(session, "demo", "secondary")
        assert loc is not None and not loc.accept_external_changes
        assert models.get_storage_location(session, "demo", "nope") is None
        assert models.get_project(session, "demo") is not None
        assert models.get_project(session, "nope") is None


@pytest.mark.req("SVC-02")
def test_set_copy_state(ctx: AppContext) -> None:
    with ctx.write_session() as session:
        copy_id = models.register_file(
            session, project="demo", storage_location="primary", path="a", hash="h", size=1
        ).copy_id
    with ctx.write_session() as session:
        models.set_copy_state(session, copy_id, FileState.UNKNOWN)
    with ctx.sessionmaker() as session:
        copy = models.get_copy(session, copy_id)
        assert copy is not None and copy.state == FileState.UNKNOWN
    with ctx.write_session() as session, pytest.raises(models.NotFoundError):
        models.set_copy_state(session, 999, FileState.UNKNOWN)

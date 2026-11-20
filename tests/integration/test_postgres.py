"""The migrations and the ORM on a real Postgres (DB-01b).

Each test gets a fresh database in the session's Postgres container.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta, timezone

import pytest
from alembic import command
from sqlalchemy import BigInteger, inspect, select, text
from sqlalchemy.exc import IntegrityError

from heliopuddle.server import models
from heliopuddle.server.database.engine import create_engine_for, make_sessionmaker
from heliopuddle.server.database.migrate import (
    alembic_config,
    current_revision,
    head_revision,
    is_at_head,
    schema_diffs,
    upgrade_to_head,
)
from heliopuddle.server.database.orm import (
    File,
    FileStorageLocation,
    IndexProgress,
    Project,
    RuleState,
    Series,
    StorageLocation,
)
from heliopuddle.server.models.series import _version_order  # pyright: ignore[reportPrivateUsage]
from heliopuddle.server.schemas import FileState

TABLES = {"project", "storage_location", "series", "file", "file_storage_location"}


@pytest.mark.req("DB-01b", "DB-05")
def test_migrations_create_a_schema_matching_the_models(postgres_url: str) -> None:
    assert current_revision(postgres_url) is None

    upgrade_to_head(postgres_url)

    assert is_at_head(postgres_url)
    assert current_revision(postgres_url) == head_revision()
    assert head_revision() is not None
    assert schema_diffs(postgres_url) == []

    engine = create_engine_for(postgres_url)
    try:
        insp = inspect(engine)
        assert set(insp.get_table_names()) >= TABLES | {"alembic_version"}
        ids = {c["name"]: c["type"] for c in insp.get_columns("file")}
        assert isinstance(ids["id"], BigInteger)
        assert "updated_by" in ids
        indexes = {i["name"]: i for i in insp.get_indexes("file")}
        live = indexes["uq_file_project_path_live"]
        assert live["unique"]
        assert live["column_names"] == ["project_id", "path"]
        assert "deleted_at IS NULL" in live["dialect_options"]["postgresql_where"]
        assert "ix_file_series_id" in indexes
    finally:
        engine.dispose()


@pytest.mark.req("DB-06")
def test_rule_state_and_index_cursors_round_trip_on_postgres(postgres_url: str) -> None:
    engine = create_engine_for(postgres_url)
    moment = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
    try:
        upgrade_to_head(engine)
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO project (id, name) VALUES (1, 'p')"))
            conn.execute(
                text(
                    "INSERT INTO storage_location "
                    "(id, project_id, name, url, accept_external_changes, active) "
                    "VALUES (1, 1, 'primary', 'file:///store', true, true)"
                )
            )
            conn.execute(
                text("INSERT INTO series (id, project_id, series_key) VALUES (1, 1, 'p:a')")
            )
            conn.execute(
                text(
                    "INSERT INTO file (id, project_id, series_id, path, name, size, hash, "
                    "hash_algo, is_latest, created_at, updated_at) VALUES "
                    "(1, 1, 1, 'a', 'a', 1, 'h', 'md5', true, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO index_progress (storage_location_id, last_full_at) VALUES (1, :at)"
                ),
                {"at": moment},
            )
        assert current_revision(engine) == head_revision()
        assert schema_diffs(engine) == []
        sessions = make_sessionmaker(engine)
        with sessions.begin() as session:
            file = session.get_one(File, 1)
            assert file.path == "a" and file.ignored_from_all_rules is False
            progress = session.get_one(IndexProgress, 1)
            assert progress.last_full_at == moment
            progress.full_started_at = moment
            progress.full_cursor_path = "a"
            progress.incremental_cursor_at = moment
            progress.incremental_cursor_id = 1
            session.add(
                RuleState(
                    project_id=1,
                    rule_name="archive",
                    enabled=False,
                    watermark_at=moment,
                    watermark_id=1,
                    last_success_at=moment,
                )
            )
        with sessions() as session:
            state = session.get_one(RuleState, (1, "archive"))
            assert not state.enabled
            assert (state.watermark_at, state.watermark_id) == (moment, 1)
            progress = session.get_one(IndexProgress, 1)
            assert progress.incremental_cursor_at == moment
            assert progress.full_cursor_path == "a"
    finally:
        engine.dispose()


@pytest.mark.req("DB-01b")
def test_upgrade_is_idempotent_and_reversible(postgres_url: str) -> None:
    upgrade_to_head(postgres_url)
    upgrade_to_head(postgres_url)  # already at head: no-op

    engine = create_engine_for(postgres_url)
    try:
        with engine.begin() as conn:
            command.downgrade(alembic_config(conn), "base")
        assert current_revision(engine) is None
        assert set(inspect(engine).get_table_names()) == {"alembic_version"}
    finally:
        engine.dispose()

    upgrade_to_head(postgres_url)
    assert schema_diffs(postgres_url) == []


@pytest.mark.req("DB-01b")
def test_concurrent_upgrades_are_serialised(postgres_url: str) -> None:
    errors: list[BaseException] = []
    barrier = threading.Barrier(4)

    def upgrade() -> None:
        try:
            barrier.wait()
            upgrade_to_head(postgres_url)
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=upgrade) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)

    assert errors == []
    assert is_at_head(postgres_url)
    assert schema_diffs(postgres_url) == []


@pytest.mark.req("DB-01b")
def test_orm_round_trip_on_postgres(postgres_url: str) -> None:
    upgrade_to_head(postgres_url)
    engine = create_engine_for(postgres_url)
    sessions = make_sessionmaker(engine)
    cet = timezone(timedelta(hours=1))
    try:
        with sessions.begin() as s:
            project = Project(name="p")
            location = StorageLocation(project=project, name="primary", url="file:///s")
            s.add_all([project, location])
            s.flush()
            series = Series(project_id=project.id, series_key="a_v{v}.cdf")
            s.add(series)
            s.flush()
            f = File(
                project=project,
                path="a_v1.cdf",
                name="a_v1.cdf",
                series=series,
                version_number=1,
                content_date=datetime(2024, 1, 2, 4, tzinfo=cet),
                size=3,
                hash="abc",
                file_metadata={"k": [1, "two", None]},
                created_by="alice",
            )
            s.add(f)
            s.flush()
            s.add(FileStorageLocation(file=f, storage_location=location, state=FileState.PENDING))
            series.latest_file_id = f.id
            file_id = f.id

        with sessions() as s:
            f = s.get_one(File, file_id)
            assert f.content_date == datetime(2024, 1, 2, 3, tzinfo=UTC)
            assert f.content_date.tzinfo is UTC
            assert f.created_at.tzinfo is UTC
            assert f.file_metadata == {"k": [1, "two", None]}
            assert f.is_latest is True
            assert f.hash_algo == "md5"
            [copy] = f.copies
            assert copy.state is FileState.PENDING
            assert copy.storage_location.active is True
            assert f.series.latest_file_id == file_id
            # the state is stored as its value in a VARCHAR, as on SQLite
            raw = s.execute(text("SELECT state FROM file_storage_location")).scalar_one()
            assert raw == "pending"
    finally:
        engine.dispose()


@pytest.mark.req("DB-01b")
def test_live_paths_are_unique_but_deleted_rows_are_kept(postgres_url: str) -> None:
    upgrade_to_head(postgres_url)
    engine = create_engine_for(postgres_url)
    sessions = make_sessionmaker(engine)

    def new_file(project_id: int, series_id: int) -> File:
        return File(
            project_id=project_id,
            path="x.csv",
            name="x.csv",
            series_id=series_id,
            size=1,
            hash="h",
        )

    try:
        with sessions.begin() as s:
            project = Project(name="p")
            s.add(project)
            s.flush()
            series = Series(project_id=project.id, series_key="x.csv")
            s.add(series)
            s.flush()
            first = new_file(project.id, series.id)
            s.add(first)
        with pytest.raises(IntegrityError), sessions.begin() as s:
            s.add(new_file(project.id, series.id))

        with sessions.begin() as s:
            s.get_one(File, first.id).deleted_at = datetime.now(UTC)
        with sessions.begin() as s:
            s.add(new_file(project.id, series.id))
        with sessions() as s:
            count = s.execute(text("SELECT count(*) FROM file WHERE path = 'x.csv'")).scalar()
            assert count == 2
    finally:
        engine.dispose()


@pytest.mark.req("DB-07")
def test_version_columns_indexes_and_check_on_postgres(postgres_url: str) -> None:
    upgrade_to_head(postgres_url)
    engine = create_engine_for(postgres_url)
    try:
        assert schema_diffs(engine) == []
        with engine.connect() as conn:
            collation = conn.execute(
                text(
                    "SELECT collation_name FROM information_schema.columns"
                    " WHERE table_name = 'file' AND column_name = 'version_text'"
                )
            ).scalar_one()
            indexes = dict(
                conn.execute(
                    text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'file'")
                ).all()
            )
        assert collation == "C"
        assert "(series_id, version_number DESC)" in indexes["ix_file_series_version_number"]
        assert "(series_id, version_text DESC)" in indexes["ix_file_series_version_text"]
        columns = {c["name"] for c in inspect(engine).get_columns("file")}
        assert not columns & {"version", "version_major", "version_minor"}
        assert "version_type" in {c["name"] for c in inspect(engine).get_columns("series")}
    finally:
        engine.dispose()


@pytest.mark.req("DB-08")
def test_arrived_at_is_required_on_postgres(postgres_url: str) -> None:
    upgrade_to_head(postgres_url)
    engine = create_engine_for(postgres_url)
    try:
        assert current_revision(engine) == head_revision()
        assert schema_diffs(engine) == []
        columns = {c["name"]: c for c in inspect(engine).get_columns("file_storage_location")}
        assert columns["arrived_at"]["nullable"] is False
    finally:
        engine.dispose()


@pytest.mark.req("DB-07", "API-02")
def test_text_versions_order_byte_wise_and_one_version_only_on_postgres(
    postgres_url: str,
) -> None:
    upgrade_to_head(postgres_url)
    engine = create_engine_for(postgres_url)
    sessions = make_sessionmaker(engine)
    try:
        with sessions.begin() as s:
            project = Project(name="p")
            location = StorageLocation(project=project, name="primary", url="file:///s")
            s.add_all([project, location])
            s.flush()
            series = Series(project_id=project.id, series_key="v:t", version_type="text")
            s.add(series)
            s.flush()
            # The default collation would ignore punctuation and case ("a-c" < "a.b").
            texts = ["001.0002", "001.0010", "a.b", "a-c", "B", "a"]
            files = [
                File(
                    project=project,
                    path=f"t{i}",
                    name=f"t{i}",
                    series=series,
                    version_text=v,
                    size=1,
                    hash="h",
                    is_latest=False,
                )
                for i, v in enumerate(texts)
            ]
            s.add_all(files)
            s.flush()
            s.add_all(
                FileStorageLocation(file=f, storage_location=location, state=FileState.AVAILABLE)
                for f in files
            )
            series_id = series.id
        with sessions() as s:
            ordered = s.scalars(
                select(File.version_text)
                .where(File.series_id == series_id)
                .order_by(*_version_order("text"))
            ).all()
        assert ordered == sorted(texts, key=str.encode, reverse=True)
        assert ordered.index("001.0010") < ordered.index("001.0002")
        assert ordered.index("a.b") < ordered.index("a-c")
        with sessions.begin() as s:
            latest = models.recompute_latest(s, models.lock_series_by_id(s, series_id))
            assert s.get_one(File, latest).version_text == "a.b"  # type: ignore[arg-type]

        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(text("UPDATE file SET version_number = 1 WHERE path = 't0'"))
    finally:
        engine.dispose()

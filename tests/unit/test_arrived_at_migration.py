"""The ``arrived_at`` column of the initial migration (DB-08)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import inspect, select, text

from heliopuddle.server.database.engine import create_engine_for, make_sessionmaker
from heliopuddle.server.database.migrate import schema_diffs, upgrade_to_head
from heliopuddle.server.database.orm import File, FileStorageLocation, Project, Series
from heliopuddle.server.database.orm import StorageLocation as StorageLocationRow


@pytest.mark.req("DB-08")
def test_arrived_at_is_a_required_column_that_round_trips(tmp_path: Path) -> None:
    engine = create_engine_for(f"sqlite:///{tmp_path / 'hp.db'}")
    try:
        upgrade_to_head(engine)
        assert schema_diffs(engine) == []
        insp = inspect(engine)
        columns = {c["name"]: c for c in insp.get_columns("file_storage_location")}
        assert columns["arrived_at"]["nullable"] is False
        assert "ix_file_storage_location_updated_at_id" in {
            i["name"] for i in insp.get_indexes("file_storage_location")
        }
        arrived = datetime(2026, 2, 3, 4, 5, 6, tzinfo=UTC)
        with make_sessionmaker(engine)() as session:
            project = Project(name="demo")
            session.add(project)
            session.flush()
            location = StorageLocationRow(
                project_id=project.id,
                name="primary",
                url="file:///x",
                accept_external_changes=True,
                active=True,
            )
            series = Series(project_id=project.id, series_key="p:a.txt")
            session.add_all([location, series])
            session.flush()
            file = File(
                project_id=project.id,
                path="a.txt",
                name="a.txt",
                series_id=series.id,
                size=1,
                hash="h",
                hash_algo="md5",
                is_latest=True,
                created_at=arrived,
                updated_at=arrived,
            )
            session.add(file)
            session.flush()
            session.add(
                FileStorageLocation(
                    file_id=file.id,
                    storage_location_id=location.id,
                    state="available",
                    updated_at=arrived,
                    arrived_at=arrived,
                )
            )
            session.commit()
            copy = session.scalars(select(FileStorageLocation)).one()
            assert copy.arrived_at == arrived
        with engine.begin() as conn, pytest.raises(Exception, match="arrived_at"):
            conn.execute(
                text(
                    "INSERT INTO file_storage_location (file_id, storage_location_id, state,"
                    " generation, updated_at) VALUES (1, 1, 'available', 0, '2026-01-01')"
                )
            )
    finally:
        engine.dispose()

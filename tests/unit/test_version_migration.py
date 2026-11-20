"""Number and text versions in the schema (DB-07)."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from heliopuddle.server.database.engine import create_engine_for
from heliopuddle.server.database.migrate import schema_diffs, upgrade_to_head


@pytest.mark.req("DB-07")
def test_schema_has_number_and_text_versions_and_allows_at_most_one(tmp_path: Path) -> None:
    engine = create_engine_for(f"sqlite:///{tmp_path / 'hp.db'}")
    try:
        upgrade_to_head(engine)
        assert schema_diffs(engine) == []
        insp = inspect(engine)
        columns = {c["name"] for c in insp.get_columns("file")}
        assert {"version_number", "version_text", "path_lower"} <= columns
        assert not columns & {"version", "version_major", "version_minor"}
        series_columns = {c["name"] for c in insp.get_columns("series")}
        assert "version_type" in series_columns
        assert "latest_by" not in series_columns
        assert {i["name"] for i in insp.get_indexes("file")} >= {
            "ix_file_series_version_number",
            "ix_file_series_version_text",
            "uq_file_project_path_live",
        }
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO project (name) VALUES ('demo')"))
            conn.execute(text("INSERT INTO series (project_id, series_key) VALUES (1, 'k')"))
            conn.execute(
                text(
                    "INSERT INTO file (project_id, path, name, series_id, version_number, size,"
                    " hash, hash_algo, is_latest, created_at, updated_at) VALUES (1, 'a', 'a',"
                    " 1, 1, 1, 'h', 'md5', 1, '2026-01-01', '2026-01-01')"
                )
            )
        # The CHECK allows at most one version.
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(text("UPDATE file SET version_text = 'x' WHERE id = 1"))
        with engine.begin() as conn:
            conn.execute(text("UPDATE file SET version_number = NULL, version_text = 'x'"))
    finally:
        engine.dispose()

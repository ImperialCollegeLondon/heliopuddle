"""Generated lower-case path and name columns for search (API-16)."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from heliopuddle.server.database.engine import create_engine_for
from heliopuddle.server.database.migrate import upgrade_to_head


@pytest.mark.req("API-16")
def test_generated_lower_case_columns_follow_path_and_name(tmp_path: Path) -> None:
    engine = create_engine_for(f"sqlite:///{tmp_path / 'hp.db'}")
    try:
        upgrade_to_head(engine)
        columns = {c["name"] for c in inspect(engine).get_columns("file")}
        assert {"path_lower", "name_lower"} <= columns
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO project (name) VALUES ('demo')"))
            conn.execute(text("INSERT INTO series (project_id, series_key) VALUES (1, 'k')"))
            conn.execute(
                text(
                    "INSERT INTO file (project_id, path, name, series_id, size, hash, hash_algo,"
                    " is_latest, created_at, updated_at)"
                    " VALUES (1, 'Data/Scan_A.CSV', 'Scan_A.CSV', 1, 1, 'h', 'md5', 1,"
                    " '2026-01-01', '2026-01-01')"
                )
            )
            row = conn.execute(text("SELECT path_lower, name_lower FROM file")).one()
            assert tuple(row) == ("data/scan_a.csv", "scan_a.csv")
            conn.execute(text("UPDATE file SET path = 'X/Y.TXT', name = 'Y.TXT'"))
            row = conn.execute(text("SELECT path_lower, name_lower FROM file")).one()
            assert tuple(row) == ("x/y.txt", "y.txt")
    finally:
        engine.dispose()

"""Shared fixtures for the M6 UI page tests."""

from __future__ import annotations

from datetime import UTC, datetime

from heliopuddle.server.schemas import FileRead, FileState
from tests.unit._ui_helpers import (
    LATER,
    Factory,
    file_with,
    make_file,
    sl_read,
)

A, P, U, M = FileState.AVAILABLE, FileState.PENDING, FileState.UNKNOWN, FileState.MISSING


def three_locations() -> list:
    return [sl_read("ingest"), sl_read("archive"), sl_read("cold", active=False)]


def fleet() -> list[FileRead]:
    """Files over three storage locations with every interesting state."""
    return [
        file_with(1, ("ingest", A), ("archive", A)),
        file_with(2, ("ingest", A), ("archive", P)),
        file_with(3, ("ingest", U), ("archive", A), content_date=datetime(2026, 3, 4, tzinfo=UTC)),
        file_with(4, ("ingest", A), ("archive", M), ignored_from_all_rules=True),
        file_with(5, ("ingest", A), is_latest=False),
        make_file(6, state=FileState.DELETED, deleted_at=LATER),
    ]


def fleet_factory(**extra: object) -> Factory:
    return Factory(files=fleet(), storage_locations=three_locations(), **extra)  # type: ignore[arg-type]

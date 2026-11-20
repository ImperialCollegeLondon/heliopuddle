"""Shared factory, fixtures and helpers for the UI unit tests (re-exports the stub client)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from heliopuddle.server.schemas import (
    FilePreview,
    FileRead,
    FileState,
    FileStorageLocationRead,
    RuleRead,
    StorageLocationRead,
)
from heliopuddle.server.ui import (
    create_ui,
)
from heliopuddle.settings import Settings
from tests.unit._ui_stub import (  # noqa: F401 - re-exported for the UI tests
    LATER,
    NOW,
    RUN_ID,
    SAME_ORIGIN,
    UI_DIR,
    StubClient,
    _states,
    copy,
    file_with,
    make_file,
    sl_read,
)


@dataclass
class Factory:
    """A client factory that hands out StubClients and remembers them."""

    projects: list[str] = field(default_factory=lambda: ["demo"])
    files: list[FileRead] = field(default_factory=list)
    error: Exception | None = None
    storage_locations: list[StorageLocationRead] | None = None
    action_error: Exception | None = None
    made: list[StubClient] = field(default_factory=list)
    rules: list[RuleRead] = field(default_factory=list)
    summary_error: Exception | None = None
    previews: dict[int, FilePreview] = field(default_factory=dict)
    preview_error: Exception | None = None
    content: bytes | None = None

    def configure(self, client: StubClient) -> StubClient:
        if self.storage_locations is not None:
            client.storage_locations = self.storage_locations
        client.previews = self.previews
        client.preview_error = self.preview_error
        if self.content is not None:
            client.content = self.content
        client.action_error = self.action_error
        client.rules = self.rules
        client.summary_error = self.summary_error
        self.made.append(client)
        return client

    def __call__(
        self,
        project: str | None,
        user: str | None = None,
        *,
        token: str | None = None,
        username: str | None = None,
        password: str | None = None,
    ) -> Any:
        client = StubClient(project, user, list(self.projects), self.files, self.error)
        client.token = token
        return self.configure(client)

    def calls(self, name: str) -> list[Any]:
        """The arguments of every call of ``name`` across the clients made."""
        return [args for c in self.made for n, args in c.calls if n == name]


@pytest.fixture(autouse=True)
def _no_prefect_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PREFECT_API_URL", raising=False)


def ui_client(factory: Factory, **settings: Any) -> TestClient:
    """A UI test client in ``header`` auth mode (no login), as before M5A."""
    app = create_ui(Settings(**{"auth_mode": "header", **settings}), client_factory=factory)
    return TestClient(app, raise_server_exceptions=False)


def find_kwargs(**overrides: Any) -> dict[str, Any]:
    """The keyword arguments the Files page passes to ``find`` with no filters set."""
    base: dict[str, Any] = {
        "path": None,
        "latest_only": False,
        "state": None,
        "storage_location": None,
        "content_date_from": None,
        "content_date_to": None,
        "include_deleted": False,
        "deleted_only": False,
        "ignored_from_all_rules": None,
        "limit": 50,
        "offset": 0,
    }
    return {**base, **overrides}


def files_find(factory: Factory) -> list[dict[str, Any]]:
    """The ``find`` calls of the Files page."""
    return factory.calls("find")


def section(html: str, marker: str, end: str = "</section>") -> str:
    """The HTML from ``marker`` to the next ``end``."""
    assert marker in html, marker
    return html.split(marker, 1)[1].split(end, 1)[0]


def table_body(html: str) -> str:
    return section(html, '<table class="ft">', "</table>").split("<tbody>", 1)[1]


def rows_by_id(html: str) -> dict[str, str]:
    rows = table_body(html).split("<tr")[1:]
    return {row.split('data-file="')[1].split('"')[0]: row for row in rows}


def make_version(
    i: int,
    *,
    version: int | None = None,
    text: str | None = None,
    is_latest: bool = False,
    deleted: bool = False,
    project: str = "demo",
    **extra: Any,
) -> FileRead:
    """A file in series 7 with the given number ``version`` or ``text`` version."""
    label = text if text is not None else f"{version or 0:03d}"
    return FileRead(
        id=i,
        project=project,
        path=f"data/x_v{label}.csv",
        name=f"x_v{label}.csv",
        series_id=7,
        version_number=version,
        version_text=text,
        size=2048 + i,
        hash=f"hash{i}",
        hash_algo="md5",
        is_latest=is_latest,
        created_at=NOW,
        updated_at=LATER,
        deleted_at=LATER if deleted else None,
        **extra,
    )


def series_files() -> list[FileRead]:
    copies = [
        FileStorageLocationRead(
            storage_location="primary",
            state=FileState.AVAILABLE,
            size=2050,
            mtime=NOW,
            last_seen_at=LATER,
            updated_at=LATER,
        ),
        FileStorageLocationRead(
            storage_location="archive", state=FileState.MISSING, updated_at=LATER
        ),
    ]
    return [
        make_version(1, version=1, deleted=True),
        make_version(
            2,
            version=2,
            content_date=datetime(2025, 12, 31, tzinfo=UTC),
            created_by="alice",
            updated_by="bob",
            metadata={
                "instrument_mode": "survey",
                "<b>key</b>": "<script>x</script>",
                "n": 3,
                "nested": {"a": [1, 2]},
            },
            storage_locations=copies,
        ),
        make_version(3, version=3, is_latest=True),
    ]


def filter_files() -> list[FileRead]:
    return [
        make_version(1, version=1, deleted=True),
        make_version(2, version=2),
        make_version(3, version=3, is_latest=True),
        make_file(4, state=FileState.MISSING),
    ]


FORBIDDEN = (
    "heliopuddle.server.database",
    "heliopuddle.server.models",
    "heliopuddle.server.context",
    "sqlalchemy",
)


INDEXED = datetime(2026, 4, 5, 6, 7, 8, tzinfo=UTC)


def m3_storage_locations() -> list[StorageLocationRead]:
    return [
        sl_read(
            "primary",
            url="file:///srv/primary",
            accept_external_changes=True,
            last_full_index_at=INDEXED,
        ),
        sl_read(
            "archive",
            url="",
            accept_external_changes=False,
            last_incremental_index_at=INDEXED,
        ),
        sl_read("old", active=False),
    ]


def notice_of(html: str) -> str:
    return section(html, 'class="notice" role="status"', "</div>")


def unknown_file() -> FileRead:
    return file_with(
        2,
        ("primary", FileState.AVAILABLE),
        ("archive", FileState.UNKNOWN),
        ("old", FileState.UNKNOWN),
    )

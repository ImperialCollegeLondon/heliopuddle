"""Files API listing, filters and series versions."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from heliopuddle.server import models
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import File
from heliopuddle.server.schemas import FileState
from tests.unit import _files_api_helpers as _fixtures

client = _fixtures.client
config_dir = _fixtures.config_dir


# (path, storage location, final copy state, deleted)
GOLDEN: list[tuple[str, str, FileState, bool]] = [
    ("sci/l1/a_20250101_v01.cdf", "primary", FileState.AVAILABLE, False),  # 0
    ("sci/l1/a_20250101_v02.cdf", "primary", FileState.AVAILABLE, False),  # 1
    ("sci/l1/b_20250102_v01.cdf", "primary", FileState.PENDING, False),  # 2
    ("sci/l2/a_20250101_v01.cdf", "primary", FileState.UNKNOWN, False),  # 3
    ("cal/x_v1.2.json", "primary", FileState.AVAILABLE, False),  # 4
    ("cal/x_v1.3.json", "primary", FileState.AVAILABLE, True),  # 5
    ("notes/readme.txt", "secondary", FileState.PENDING, False),  # 6
    ("odd/100%_done_v1.txt", "primary", FileState.AVAILABLE, False),  # 7
    ("odd/100x_done_v1.txt", "primary", FileState.AVAILABLE, False),  # 8
]


@pytest.fixture
def golden(ctx: AppContext) -> list[int]:
    """Register the golden dataset (in order) and return the file ids."""
    rules = ctx.projects["demo"].versioning
    ids: list[int] = []
    for path, location, state, deleted in GOLDEN:
        with ctx.write_session() as session:
            result = models.register_file(
                session,
                project="demo",
                storage_location=location,
                path=path,
                hash="h",
                size=1,
                rules=rules,
            )
            if state != FileState.PENDING:
                models.set_copy_state(session, result.copy_id, state)
        if deleted:
            with ctx.write_session() as session:
                _, copies = models.begin_delete(session, result.file_id)
            with ctx.write_session() as session:
                models.finish_delete(session, result.file_id, copies)
        ids.append(result.file_id)
    return ids


def _query(client: TestClient, **params: Any) -> tuple[list[int], int]:
    r = client.get("/projects/demo/files", params=params)
    assert r.status_code == 200, r.text
    body = r.json()
    return [f["id"] for f in body["items"]], body["total"]


def _newest_first(golden: list[int], *indexes: int) -> list[int]:
    return sorted((golden[i] for i in indexes), reverse=True)


@pytest.mark.req("API-09")
@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({}, [0, 1, 2, 3, 4, 6, 7, 8]),
        ({"include_deleted": True}, [0, 1, 2, 3, 4, 5, 6, 7, 8]),
        ({"path": "sci/**"}, [0, 1, 2, 3]),
        ({"path": "sci/*/a_*"}, [0, 1, 3]),
        ({"path": "sci/l?/b_*.cdf"}, [2]),
        ({"path": "**/*.json"}, [4]),
        ({"path": "**/*.json", "include_deleted": True}, [4, 5]),
        ({"path": "*"}, []),
        ({"path": "**"}, [0, 1, 2, 3, 4, 6, 7, 8]),
        ({"path": "notes/readme.txt"}, [6]),
        ({"path": "notes/readme"}, []),
        ({"path": "odd/100%_*"}, [7]),
        ({"path": "odd/100?_*"}, [7, 8]),
        # 3 has only an unknown copy, so it is not eligible to be latest (API-12).
        ({"latest_only": True}, [1, 2, 4, 6, 7, 8]),
        ({"state": "available"}, [0, 1, 4, 7, 8]),
        ({"state": "pending"}, [2, 6]),
        ({"state": "unknown"}, [3]),
        ({"state": "deleted", "include_deleted": True}, [5]),
        ({"storage_location": "secondary"}, [6]),
        ({"storage_location": "primary", "state": "pending"}, [2]),
        ({"storage_location": "primary", "include_deleted": True}, [0, 1, 2, 3, 4, 7, 8]),
        ({"storage_location": "nope"}, []),
        ({"content_date_from": "2025-01-02T00:00:00Z"}, [2]),
        ({"content_date_to": "2025-01-01T00:00:00Z"}, [0, 1, 3]),
        (
            {"content_date_from": "2025-01-01T00:00:00Z", "content_date_to": "2025-01-02"},
            [0, 1, 2, 3],
        ),
        ({"path": "sci/**", "latest_only": True, "state": "available"}, [1]),
        ({"path": "**/a_*", "content_date_to": "2025-01-01T12:00:00Z"}, [0, 1, 3]),
    ],
)
def test_list_filters(
    client: TestClient, golden: list[int], params: dict[str, Any], expected: list[int]
) -> None:
    ids, total = _query(client, **params)
    assert ids == _newest_first(golden, *expected)
    assert total == len(expected)


@pytest.mark.req("API-09")
def test_list_filter_by_series(client: TestClient, golden: list[int]) -> None:
    series_id = client.get(f"/files/{golden[0]}").json()["series_id"]
    assert _query(client, series_id=series_id) == (_newest_first(golden, 0, 1), 2)
    assert _query(client, series_id=series_id, latest_only=True) == ([golden[1]], 1)


@pytest.mark.req("API-09")
@pytest.mark.parametrize("path", [None, "**"])
def test_list_paging_and_order(client: TestClient, golden: list[int], path: str | None) -> None:
    extra = {} if path is None else {"path": path}
    live = [0, 1, 2, 3, 4, 6, 7, 8]
    by_path = [golden[i] for i in sorted(live, key=lambda i: GOLDEN[i][0])]
    pages = [
        _query(client, order="path", limit=3, offset=offset, **extra) for offset in (0, 3, 6, 9)
    ]
    assert [p[1] for p in pages] == [8, 8, 8, 8]
    assert [i for p in pages for i in p[0]] == by_path
    assert pages[3][0] == []
    newest = _query(client, limit=2, offset=1, **extra)
    assert newest == (_newest_first(golden, *live)[1:3], 8)


@pytest.mark.req("API-09")
def test_list_validation(client: TestClient) -> None:
    for params in (
        {"limit": 0},
        {"limit": 1001},
        {"offset": -1},
        {"order": "size"},
        {"state": "gone"},
    ):
        assert client.get("/projects/demo/files", params=params).status_code == 422, params
    assert client.get("/projects/demo/files", params={"limit": 1000}).json()["limit"] == 1000
    assert client.get("/projects/demo/files").json()["limit"] == 100
    assert client.get("/projects/nope/files").status_code == 404


@pytest.mark.req("API-09")
def test_like_prefix_is_escaped(ctx: AppContext, golden: list[int]) -> None:
    assert models.like_escape("a%b_c\\d") == "a\\%b\\_c\\\\d"
    with ctx.sessionmaker() as session:
        rows = session.scalars(
            select(File.path).where(File.path.like(models.like_escape("odd/100%_") + "%", "\\"))
        ).all()
    assert rows == ["odd/100%_done_v1.txt"]


@pytest.mark.req("API-09")
def test_series_versions(client: TestClient, golden: list[int]) -> None:
    rec = client.get(f"/files/{golden[0]}").json()
    r = client.get(f"/series/{rec['series_id']}/versions")
    assert r.status_code == 200
    body = r.json()
    assert body["series_key"] == "sci/l1/a_20250101_v{v}.cdf"
    assert body["project"] == "demo"
    assert [f["id"] for f in body["items"]] == [golden[1], golden[0]]
    assert [f["is_latest"] for f in body["items"]] == [True, False]

    cal = client.get(f"/files/{golden[4]}").json()["series_id"]
    assert [f["id"] for f in client.get(f"/series/{cal}/versions").json()["items"]] == [golden[4]]
    with_deleted = client.get(f"/series/{cal}/versions", params={"include_deleted": True}).json()
    assert [f["id"] for f in with_deleted["items"]] == [golden[5], golden[4]]
    assert client.get("/series/9999/versions").status_code == 404

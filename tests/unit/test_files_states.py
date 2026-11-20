"""``GET /projects/{p}/files`` with a repeated ``state`` filter (API-09, from M6)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from heliopuddle.server import models
from heliopuddle.server.api import create_api
from heliopuddle.server.context import AppContext
from heliopuddle.server.schemas import FileState
from tests.golden_files import add_file

A, P, M, U, X = (
    FileState.AVAILABLE,
    FileState.PENDING,
    FileState.MISSING,
    FileState.UNKNOWN,
    FileState.DELETED,
)


@pytest.fixture
def client(ctx: AppContext) -> Iterator[TestClient]:
    with TestClient(create_api(ctx)) as c:
        yield c


@pytest.fixture
def dataset(ctx: AppContext) -> None:
    with ctx.write_session() as session:
        add_file(session, "demo", "ok.txt", minute=1, copies={"primary": A, "secondary": A})
        add_file(session, "demo", "miss.txt", minute=2, copies={"primary": M, "secondary": A})
        add_file(session, "demo", "unk.txt", minute=3, copies={"primary": A, "secondary": U})
        add_file(session, "demo", "pend.txt", minute=4, copies={"primary": P})
        add_file(session, "demo", "gone.txt", minute=5, copies={"primary": X, "secondary": M})
        add_file(
            session, "demo", "old.txt", minute=6, deleted=True, copies={"primary": M}
        )  # fmt: skip


def _paths(client: TestClient, **params: Any) -> list[str]:
    r = client.get("/projects/demo/files", params=params)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == len(body["items"])
    return [item["path"] for item in body["items"]]


@pytest.mark.req("API-09")
@pytest.mark.usefixtures("dataset")
def test_repeated_state_matches_a_copy_in_any_of_them(client: TestClient) -> None:
    assert _paths(client, state=["missing", "unknown"]) == ["gone.txt", "unk.txt", "miss.txt"]
    assert _paths(client, state=["pending", "missing", "unknown"]) == [
        "gone.txt",
        "pend.txt",
        "unk.txt",
        "miss.txt",
    ]
    assert _paths(client, state=["missing", "unknown"], include_deleted=True)[0] == "old.txt"


@pytest.mark.req("API-09")
@pytest.mark.usefixtures("dataset")
def test_single_state_is_unchanged(client: TestClient) -> None:
    assert _paths(client, state="missing") == ["gone.txt", "miss.txt"]
    assert _paths(client, state=["missing"]) == ["gone.txt", "miss.txt"]
    assert _paths(client, state="missing", storage_location="primary") == ["miss.txt"]


@pytest.mark.req("API-09")
@pytest.mark.usefixtures("dataset")
def test_repeated_state_with_a_storage_location(client: TestClient) -> None:
    assert _paths(client, state=["missing", "unknown"], storage_location="secondary") == [
        "gone.txt",
        "unk.txt",
    ]
    assert _paths(client, state=["missing", "deleted"], storage_location="primary") == [
        "gone.txt",
        "miss.txt",
    ]


@pytest.mark.req("API-09")
@pytest.mark.usefixtures("dataset")
def test_repeated_state_pages_and_totals(client: TestClient) -> None:
    r = client.get(
        "/projects/demo/files", params={"state": ["missing", "unknown"], "limit": 2, "offset": 2}
    )
    body = r.json()
    assert body["total"] == 3
    assert [i["path"] for i in body["items"]] == ["miss.txt"]


@pytest.mark.req("API-09")
def test_bad_state_is_422(client: TestClient) -> None:
    r = client.get("/projects/demo/files", params={"state": ["missing", "bogus"]})
    assert r.status_code == 422


@pytest.mark.req("API-09")
@pytest.mark.usefixtures("dataset")
def test_model_accepts_one_state_a_sequence_or_none(ctx: AppContext) -> None:
    with ctx.sessionmaker() as session:
        _, one = models.list_files(session, "demo", state=FileState.UNKNOWN)
        _, two = models.list_files(session, "demo", state=(FileState.UNKNOWN, FileState.PENDING))
        _, empty = models.list_files(session, "demo", state=[])
        _, none = models.list_files(session, "demo", state=None)
    assert (one, two, empty, none) == (1, 2, 5, 5)


@pytest.mark.req("API-09")
@pytest.mark.usefixtures("dataset")
def test_deleted_only_lists_just_the_deleted_files(client: TestClient) -> None:
    assert _paths(client, deleted_only=True) == ["old.txt"]
    assert _paths(client, deleted_only=True, include_deleted=False) == ["old.txt"]
    assert _paths(client, deleted_only=True, state="available") == []

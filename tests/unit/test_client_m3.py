"""HelioPuddleClient M3: ``trigger_index`` and ``accept_file`` (CLI-07).

``trigger_index`` runs against the in-process API with run submission mocked.
``accept_file`` runs against an ``httpx.MockTransport`` standing in for the
accept endpoint.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from heliopuddle.client import HelioPuddleClient, HelioPuddleError, NotFoundError
from heliopuddle.server import models
from heliopuddle.server.api import create_api
from heliopuddle.server.context import AppContext
from heliopuddle.server.schemas import FileRead, FileState, FileStorageLocationRead, RunInfo
from heliopuddle.server.services import runs

API_URL = "http://testserver"
RUN_ID = uuid.UUID("11111111-2222-4333-8444-555555555555")
NOW = datetime(2026, 5, 6, 7, 8, 9, tzinfo=UTC)


@dataclasses.dataclass
class FakeFlowRun:
    id: uuid.UUID


@pytest.fixture
def submitted(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    names: list[str] = []

    def fake(name: str, **kwargs: Any) -> FakeFlowRun:
        names.append(name)
        return FakeFlowRun(RUN_ID)

    monkeypatch.setattr(runs, "run_deployment", fake)
    monkeypatch.setenv("PREFECT_API_URL", "http://prefect:4200/api")
    return names


@pytest.fixture
def hp(ctx: AppContext) -> Iterator[HelioPuddleClient]:
    with (
        TestClient(create_api(ctx)) as http,
        HelioPuddleClient(API_URL, "demo", user="alice", http_client=http) as c,
    ):
        yield c


# --- trigger_index ---------------------------------------------------------


@pytest.mark.req("CLI-07")
def test_trigger_index_defaults(hp: HelioPuddleClient, submitted: list[str]) -> None:
    info = hp.trigger_index()
    assert isinstance(info, RunInfo)
    assert info.flow_run_id == str(RUN_ID)
    assert info.flow_run_url == f"http://prefect:4200/runs/flow-run/{RUN_ID}"
    assert submitted == ["index_full/index-full-p1-sl1-demo-primary"]


@pytest.mark.req("CLI-07")
def test_trigger_index_named_storage_location_incremental(
    hp: HelioPuddleClient, submitted: list[str]
) -> None:
    info = hp.trigger_index("secondary", mode="incremental")
    assert info.deployment == "index_incremental/index-incremental-p1-sl2-demo-secondary"
    assert submitted == ["index_incremental/index-incremental-p1-sl2-demo-secondary"]


@pytest.mark.req("CLI-07")
def test_trigger_index_errors(hp: HelioPuddleClient, ctx: AppContext, submitted: list[str]) -> None:
    with pytest.raises(NotFoundError):
        hp.trigger_index("nope")
    with ctx.sessionmaker() as session:
        row = models.get_storage_location(session, "demo", "secondary")
        assert row is not None
        row.active = False
        session.commit()
    with pytest.raises(HelioPuddleError) as exc:
        hp.trigger_index("secondary")
    assert exc.value.status_code == 409
    assert submitted == []


@pytest.mark.req("CLI-07")
def test_trigger_index_needs_a_project() -> None:
    with HelioPuddleClient(API_URL, None) as c, pytest.raises(ValueError):
        c.trigger_index()


# --- accept_file -----------------------------------------------------------


def _file(state: FileState) -> dict[str, Any]:
    record = FileRead(
        id=7,
        project="demo",
        path="data/a.csv",
        name="a.csv",
        series_id=3,
        size=10,
        hash="abc",
        hash_algo="md5",
        is_latest=state == FileState.AVAILABLE,
        created_at=NOW,
        updated_at=NOW,
        updated_by="alice" if state == FileState.AVAILABLE else None,
        storage_locations=[
            FileStorageLocationRead(storage_location="secondary", state=state, updated_at=NOW)
        ],
    )
    return record.model_dump(mode="json")


@dataclasses.dataclass
class FakeAcceptApi:
    """Answers by-path and by-id lookups and ``POST /files/{id}/accept``; records requests."""

    accept_status: int = 200
    accept_body: dict[str, Any] | None = None
    requests: list[httpx.Request] = dataclasses.field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/projects/demo/files/by-path":
            if request.url.params["path"] == "data/a.csv":
                return httpx.Response(200, json=_file(FileState.UNKNOWN))
            return httpx.Response(404, json={"detail": "no live file"})
        if request.method == "GET" and request.url.path == "/files/7":
            return httpx.Response(200, json=_file(FileState.UNKNOWN))
        if request.method == "POST" and request.url.path == "/files/7/accept":
            body = self.accept_body or _file(FileState.AVAILABLE)
            return httpx.Response(self.accept_status, json=body)
        return httpx.Response(404, json={"detail": "not found"})

    def client(self) -> HelioPuddleClient:
        http = httpx.Client(base_url=API_URL, transport=httpx.MockTransport(self))
        return HelioPuddleClient(API_URL, "demo", user="bob", http_client=http)


@pytest.mark.req("CLI-07")
def test_accept_file_by_id() -> None:
    api = FakeAcceptApi()
    record = api.client().accept_file(7, "secondary")
    assert record.id == 7 and record.is_latest
    assert record.storage_locations[0].state == FileState.AVAILABLE
    lookup, request = api.requests  # the id is checked to be in the project first (CLI-08)
    assert lookup.method == "GET" and lookup.url.path == "/files/7"
    assert request.method == "POST" and request.url.path == "/files/7/accept"
    assert json.loads(request.content) == {"storage_location": "secondary"}
    assert request.headers["X-Forwarded-User"] == "bob"


@pytest.mark.req("CLI-07")
def test_accept_file_by_path_with_default_storage_location() -> None:
    api = FakeAcceptApi()
    record = api.client().accept_file("data/a.csv")
    assert record.state == FileState.AVAILABLE
    lookup, accept = api.requests
    assert lookup.url.path == "/projects/demo/files/by-path"
    assert accept.url.path == "/files/7/accept"
    assert json.loads(accept.content) == {}


@pytest.mark.req("CLI-07")
def test_accept_file_copy_not_unknown_is_409() -> None:
    api = FakeAcceptApi(accept_status=409, accept_body={"detail": "copy is available"})
    with pytest.raises(HelioPuddleError) as exc:
        api.client().accept_file(7)
    assert exc.value.status_code == 409
    assert exc.value.detail == "copy is available"


@pytest.mark.req("CLI-07")
def test_accept_file_unknown_path_is_not_found() -> None:
    api = FakeAcceptApi()
    with pytest.raises(NotFoundError):
        api.client().accept_file("data/missing.csv")
    assert [r.url.path for r in api.requests] == ["/projects/demo/files/by-path"]

"""Project summaries: ``GET /projects/{p}/summary`` (API-17).

A hand-built dataset holds copies in every state, a deleted file, an ignored
file and a storage location that is no longer in config.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from heliopuddle.server import models
from heliopuddle.server.api import create_api
from heliopuddle.server.api.deps import get_principal
from heliopuddle.server.auth.principal import Principal
from heliopuddle.server.auth.roles import ROLE_ADMIN, ROLE_READER, ROLE_WRITER
from heliopuddle.server.context import AppContext
from heliopuddle.server.schemas import FileState
from tests.golden_files import SUMMARY_EXPECTED as EXPECTED
from tests.golden_files import add_file, add_summary_dataset, states

OTHER_YAML = """\
project: other
storage_locations:
  - name: main
    url: file://{store}
"""


@pytest.fixture
def config_dir(config_dir: Path, tmp_path: Path) -> Path:
    """``demo`` (primary, secondary) and ``other`` (main)."""
    store = tmp_path / "other-store"
    store.mkdir()
    (config_dir / "other.yaml").write_text(OTHER_YAML.format(store=store))
    return config_dir


@pytest.fixture
def app(ctx: AppContext) -> Iterator[FastAPI]:
    application = create_api(ctx)
    yield application
    application.dependency_overrides.clear()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def act_as(app: FastAPI) -> Callable[..., None]:
    def act(*roles: str) -> None:
        principal = Principal("tester", tuple(roles), "local", token="tok")
        app.dependency_overrides[get_principal] = lambda: principal

    return act


@pytest.fixture
def dataset(ctx: AppContext) -> None:
    """The golden dataset (see :data:`EXPECTED`)."""
    with ctx.write_session() as session:
        add_summary_dataset(session, other="other", other_location="main")


@pytest.mark.req("API-17")
@pytest.mark.usefixtures("dataset")
def test_summary_counts_the_golden_dataset(client: TestClient) -> None:
    r = client.get("/projects/demo/summary")
    assert r.status_code == 200, r.text
    assert r.json() == EXPECTED


@pytest.mark.req("API-17")
@pytest.mark.usefixtures("dataset")
def test_summary_of_a_project_without_files_has_every_state_at_zero(
    client: TestClient, ctx: AppContext
) -> None:
    with ctx.write_session() as session:
        add_file(session, "other", "gone.txt", deleted=True, copies={"main": FileState.DELETED})
    assert client.get("/projects/other/summary").json() == {
        "project": "other",
        "files": 1,
        "latest_files": 1,
        "bytes": 1000,
        "deleted_files": 1,
        "storage_locations": [
            {
                "name": "main",
                "active": True,
                "files": 1,
                "bytes": 1000,
                "states": states(available=1),
                "no_copy": 0,
            }
        ],
        "views": {"needs_attention": 0, "in_progress": 0, "ignored": 0},
    }


@pytest.mark.req("API-17")
def test_empty_project_and_storage_locations_without_rows(ctx: AppContext) -> None:
    with ctx.sessionmaker() as session:
        empty = models.project_summary(session, "other", ["main", "fresh"])
        unknown = models.project_summary(session, "nope", ["x"])
    assert [s.name for s in empty.storage_locations] == ["main", "fresh"]
    for summary in (empty, unknown):
        assert (summary.files, summary.latest_files, summary.bytes, summary.deleted_files) == (
            0,
            0,
            0,
            0,
        )
        for location in summary.storage_locations:
            assert location.active is True
            assert location.states == dict.fromkeys(FileState, 0)
            assert (location.files, location.bytes, location.no_copy) == (0, 0, 0)
        assert summary.views.model_dump() == {"needs_attention": 0, "in_progress": 0, "ignored": 0}


@pytest.mark.req("API-17")
@pytest.mark.usefixtures("dataset")
def test_storage_locations_follow_config_order(client: TestClient, ctx: AppContext) -> None:
    cfg = ctx.projects["demo"]
    reordered = list(reversed(cfg.storage_locations))
    ctx.projects["demo"] = cfg.model_copy(update={"storage_locations": reordered})
    names = [s["name"] for s in client.get("/projects/demo/summary").json()["storage_locations"]]
    assert names == ["secondary", "primary", "old"]


@pytest.mark.req("API-17", "AUTH-08")
@pytest.mark.usefixtures("dataset")
@pytest.mark.parametrize(
    ("roles", "status"),
    [
        ((ROLE_READER,), 200),
        ((ROLE_ADMIN,), 200),
        ((f"{ROLE_READER}:demo",), 200),
        ((f"{ROLE_WRITER}:demo",), 200),
        ((f"{ROLE_READER}:other",), 404),
        ((), 404),
    ],
)
def test_summary_needs_a_role_on_the_project(
    client: TestClient, act_as: Callable[..., None], roles: tuple[str, ...], status: int
) -> None:
    act_as(*roles)
    r = client.get("/projects/demo/summary")
    assert r.status_code == status
    if status == 200:
        assert r.json() == EXPECTED
    else:
        assert r.json() == {"detail": "not found"}


@pytest.mark.req("API-17")
def test_unknown_project_is_404(client: TestClient) -> None:
    assert client.get("/projects/nope/summary").status_code == 404

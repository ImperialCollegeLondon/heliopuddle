"""HelioPuddleClient M6: search, project summaries and multi-state finds (CLI-11).

Runs against the real API in-process (header auth mode, the test default).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from heliopuddle.client import (
    HelioPuddleClient,
    HelioPuddleError,
    NotFoundError,
    ProjectSummary,
    SearchResults,
)
from heliopuddle.server.api import create_api
from heliopuddle.server.context import AppContext
from heliopuddle.server.schemas import FileState
from tests.golden_files import add_file

API_URL = "http://testserver"

OTHER_YAML = """\
project: other.v2
storage_locations:
  - name: main
    url: file://{store}
"""


@pytest.fixture
def config_dir(config_dir: Path, tmp_path: Path) -> Path:
    """``demo`` and ``other.v2`` (a name with a dot)."""
    store = tmp_path / "other-store"
    store.mkdir()
    (config_dir / "other.yaml").write_text(OTHER_YAML.format(store=store))
    return config_dir


@pytest.fixture
def http(ctx: AppContext) -> Iterator[TestClient]:
    with TestClient(create_api(ctx)) as c:
        yield c


@pytest.fixture
def hp(http: TestClient) -> Iterator[HelioPuddleClient]:
    with HelioPuddleClient(API_URL, "demo", user="alice", http_client=http) as c:
        yield c


@pytest.fixture
def dataset(ctx: AppContext) -> None:
    with ctx.write_session() as session:
        add_file(session, "demo", "x/scan_a.txt", minute=1, copies={"primary": FileState.MISSING})
        add_file(session, "demo", "x/scan_b.txt", minute=2, copies={"primary": FileState.UNKNOWN})
        add_file(session, "demo", "x/scan_c.txt", minute=3, copies={"primary": FileState.PENDING})
        add_file(session, "demo", "x/old_scan.txt", minute=0, latest=False, deleted=True)
        add_file(session, "other.v2", "y/scan_z.txt", minute=4, size=7)


@pytest.mark.req("CLI-11")
@pytest.mark.usefixtures("dataset")
def test_search_is_not_scoped_to_the_clients_project(hp: HelioPuddleClient) -> None:
    results = hp.search("scan")
    assert isinstance(results, SearchResults)
    assert [f.path for f in results.items] == [
        "y/scan_z.txt",
        "x/scan_c.txt",
        "x/scan_b.txt",
        "x/scan_a.txt",
    ]
    assert results.projects == {"demo": 3, "other.v2": 1}
    assert results.total == 4
    assert (results.limit, results.offset) == (20, 0)


@pytest.mark.req("CLI-11")
@pytest.mark.usefixtures("dataset")
def test_search_options(hp: HelioPuddleClient) -> None:
    only_other = hp.search("scan", ["other.v2"])
    assert [f.project for f in only_other.items] == ["other.v2"]
    both = hp.search("scan", projects=["demo", "other.v2"], limit=2, offset=1)
    assert [f.path for f in both.items] == ["x/scan_c.txt", "x/scan_b.txt"]
    assert both.total == 4
    assert hp.search("scan", include_deleted=True).total == 5
    assert hp.search("scan", latest_only=True, include_deleted=True).total == 4


@pytest.mark.req("CLI-11")
def test_search_errors_carry_the_status(hp: HelioPuddleClient) -> None:
    with pytest.raises(NotFoundError) as missing:
        hp.search("scan", ["nope"])
    assert missing.value.status_code == 404
    with pytest.raises(HelioPuddleError) as bad:
        hp.search("s")
    assert bad.value.status_code == 422


@pytest.mark.req("CLI-11")
@pytest.mark.usefixtures("dataset")
def test_project_summary_defaults_to_the_clients_project(hp: HelioPuddleClient) -> None:
    summary = hp.project_summary()
    assert isinstance(summary, ProjectSummary)
    assert (summary.project, summary.files, summary.deleted_files) == ("demo", 3, 1)
    assert [s.name for s in summary.storage_locations] == ["primary", "secondary"]
    assert summary.views.needs_attention == 2
    assert summary.views.in_progress == 1
    other = hp.project_summary("other.v2")
    assert (other.project, other.files, other.bytes) == ("other.v2", 1, 7)
    with pytest.raises(NotFoundError):
        hp.project_summary("nope")


@pytest.mark.req("CLI-11")
@pytest.mark.usefixtures("dataset")
def test_project_summary_without_a_project_needs_one(http: TestClient) -> None:
    with HelioPuddleClient(API_URL, None, user="alice", http_client=http) as c:
        with pytest.raises(ValueError):
            c.project_summary()
        assert c.project_summary("demo").files == 3
        assert c.search("scan").total == 4


@pytest.mark.req("CLI-11", "API-09")
@pytest.mark.usefixtures("dataset")
def test_find_accepts_one_state_or_a_list(hp: HelioPuddleClient) -> None:
    assert [f.path for f in hp.find(state="missing").items] == ["x/scan_a.txt"]
    assert [f.path for f in hp.find(state=FileState.UNKNOWN).items] == ["x/scan_b.txt"]
    page = hp.find(state=["missing", FileState.UNKNOWN])
    assert [f.path for f in page.items] == ["x/scan_b.txt", "x/scan_a.txt"]
    assert page.total == 2
    assert hp.find(state=[]).total == 3
    found = list(hp.find_all(state=("missing", "unknown", "pending"), page_size=1))
    assert [f.path for f in found] == ["x/scan_c.txt", "x/scan_b.txt", "x/scan_a.txt"]
    with pytest.raises(ValueError):
        hp.find(state=["bogus"])


@pytest.mark.req("CLI-11", "API-09")
@pytest.mark.usefixtures("dataset")
def test_find_deleted_only(hp: HelioPuddleClient) -> None:
    assert [f.path for f in hp.find(deleted_only=True).items] == ["x/old_scan.txt"]
    assert [f.path for f in hp.find_all(deleted_only=True)] == ["x/old_scan.txt"]

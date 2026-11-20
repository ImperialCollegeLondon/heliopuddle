"""Cross-project file search: ``GET /search/files`` (API-16).

Golden datasets are inserted as rows; the caller's roles are injected through
``dependency_overrides[get_principal]`` (header mode, the test default, is the
case with no override).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from heliopuddle.server import models
from heliopuddle.server.api import create_api
from heliopuddle.server.api.deps import get_principal
from heliopuddle.server.auth.principal import Principal
from heliopuddle.server.auth.roles import ROLE_ADMIN, ROLE_OPERATOR, ROLE_READER, ROLE_WRITER
from heliopuddle.server.context import AppContext
from heliopuddle.server.schemas import FileState
from tests.golden_files import add_file
from tests.local_auth import local_context

PROJECT_YAML = """\
project: {name}
storage_locations:
  - name: main
    url: file://{store}
"""

ALL = ["demo", "other", "third"]


@pytest.fixture
def config_dir(config_dir: Path, tmp_path: Path) -> Path:
    """Three projects: ``demo``, ``other`` and ``third``."""
    for name in ("other", "third"):
        store = tmp_path / f"{name}-store"
        store.mkdir()
        (config_dir / f"{name}.yaml").write_text(PROJECT_YAML.format(name=name, store=store))
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


Act = Callable[..., None]


@pytest.fixture
def act_as(app: FastAPI) -> Act:
    """Make the next requests come from a caller holding ``roles``."""

    def act(*roles: str) -> None:
        principal = Principal("tester", tuple(roles), "local", token="tok")
        app.dependency_overrides[get_principal] = lambda: principal

    return act


def _paths(body: dict[str, Any]) -> list[str]:
    return [item["path"] for item in body["items"]]


def _search(client: TestClient, q: str, **params: Any) -> dict[str, Any]:
    r = client.get("/search/files", params={"q": q, **params})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def spread(ctx: AppContext) -> None:
    """One ``alpha`` file in each project, plus a non-matching one."""
    with ctx.write_session() as session:
        for i, project in enumerate(ALL):
            add_file(session, project, f"data/alpha_{project}.txt", minute=i)
            add_file(session, project, f"data/beta_{project}.txt", minute=i)


# --- the role and project-scope matrix --------------------------------------


ROLE_CASES = [
    pytest.param((ROLE_READER,), ALL, id="global-reader"),
    pytest.param((ROLE_OPERATOR,), ALL, id="global-operator"),
    pytest.param((ROLE_ADMIN,), ALL, id="admin"),
    pytest.param((f"{ROLE_READER}:demo",), ["demo"], id="scoped-reader"),
    pytest.param(
        (f"{ROLE_WRITER}:demo", f"{ROLE_READER}:third"), ["demo", "third"], id="two-scoped"
    ),
    pytest.param(("hp-client-storage-read-write:other",), ["other"], id="scoped-storage"),
    pytest.param((), [], id="no-role"),
]


@pytest.mark.req("API-16")
@pytest.mark.usefixtures("spread")
@pytest.mark.parametrize(("roles", "visible"), ROLE_CASES)
def test_search_covers_only_the_projects_the_caller_holds_a_role_on(
    client: TestClient, act_as: Act, roles: tuple[str, ...], visible: list[str]
) -> None:
    act_as(*roles)
    body = _search(client, "alpha")
    assert sorted(item["project"] for item in body["items"]) == visible
    assert body["projects"] == dict.fromkeys(visible, 1)
    assert body["total"] == len(visible)


@pytest.mark.req("API-16")
@pytest.mark.usefixtures("spread")
@pytest.mark.parametrize(("roles", "visible"), ROLE_CASES)
def test_named_projects_must_be_readable_and_hidden_ones_look_unknown(
    client: TestClient, act_as: Act, roles: tuple[str, ...], visible: list[str]
) -> None:
    act_as(*roles)
    unknown = client.get("/search/files", params={"q": "alpha", "project": "nope"})
    assert unknown.status_code == 404
    for project in ALL:
        r = client.get("/search/files", params={"q": "alpha", "project": project})
        if project in visible:
            assert r.status_code == 200
            assert _paths(r.json()) == [f"data/alpha_{project}.txt"]
        else:
            assert r.status_code == 404
            # Identical to an unknown project apart from the name it echoes.
            assert r.json() == {"detail": f"project {project!r} not found"}
            assert unknown.json() == {"detail": "project 'nope' not found"}
    # One unreadable name among readable ones still hides everything.
    if visible and len(visible) < len(ALL):
        hidden = next(p for p in ALL if p not in visible)
        r = client.get("/search/files", params={"q": "alpha", "project": [visible[0], hidden]})
        assert r.status_code == 404


@pytest.mark.req("API-16")
@pytest.mark.usefixtures("spread")
def test_repeated_project_narrows_the_search(client: TestClient) -> None:
    body = _search(client, "alpha", project=["third", "demo", "demo"])
    assert sorted(_paths(body)) == ["data/alpha_demo.txt", "data/alpha_third.txt"]
    assert body["projects"] == {"demo": 1, "third": 1}
    assert body["total"] == 2


@pytest.mark.req("API-16")
@pytest.mark.usefixtures("spread")
def test_header_mode_searches_every_project(client: TestClient) -> None:
    body = _search(client, "alpha")
    assert body["projects"] == {"demo": 1, "other": 1, "third": 1}


@pytest.mark.req("API-16")
def test_search_needs_credentials_in_local_mode(
    tmp_path: Path, config_dir: Path, migrated_db: Path
) -> None:
    ctx = local_context(tmp_path, config_dir, migrated_db)
    try:
        with TestClient(create_api(ctx)) as c:
            r = c.get("/search/files", params={"q": "alpha"})
        assert r.status_code == 401
    finally:
        ctx.dispose()


# --- matching and ranking ---------------------------------------------------


@pytest.mark.req("API-16")
def test_ranking_name_equal_then_prefix_then_contains_then_folder(
    client: TestClient, ctx: AppContext
) -> None:
    with ctx.write_session() as session:
        # Inserted out of rank order; older files first within a rank.
        add_file(session, "demo", "orbit/zzz.txt", minute=9)  # folder only
        add_file(session, "demo", "a/my_orbit.cdf", minute=8)  # contains
        add_file(session, "demo", "b/orbit_v2.cdf", minute=1)  # prefix, older
        add_file(session, "other", "c/orbit_v3.cdf", minute=2)  # prefix, newer
        add_file(session, "demo", "d/ORBIT", minute=0)  # equal (case-insensitive)
        add_file(session, "demo", "e/unrelated.txt", minute=5)
    body = _search(client, "Orbit")
    assert _paths(body) == [
        "d/ORBIT",
        "c/orbit_v3.cdf",
        "b/orbit_v2.cdf",
        "a/my_orbit.cdf",
        "orbit/zzz.txt",
    ]
    assert body["total"] == 5
    assert body["projects"] == {"demo": 4, "other": 1}


@pytest.mark.req("API-16")
def test_ties_newest_first_then_by_id(client: TestClient, ctx: AppContext) -> None:
    with ctx.write_session() as session:
        first = add_file(session, "demo", "x/same_1.txt", minute=3)
        second = add_file(session, "demo", "x/same_2.txt", minute=3)
        older = add_file(session, "demo", "x/same_3.txt", minute=1)
    body = _search(client, "same")
    assert [item["id"] for item in body["items"]] == [second, first, older]


@pytest.mark.req("API-16")
def test_every_word_must_occur_and_the_first_word_ranks(
    client: TestClient, ctx: AppContext
) -> None:
    with ctx.write_session() as session:
        add_file(session, "demo", "mag/2025/mag_l2_burst.cdf", minute=1)
        add_file(session, "demo", "mag/2025/mag_l1_burst.cdf", minute=2)
        add_file(session, "demo", "swe/2025/swe_l2_burst.cdf", minute=3)
        add_file(session, "demo", "burst/mag_l2.cdf", minute=4)
    assert _paths(_search(client, "l2 BURST  mag")) == [
        "burst/mag_l2.cdf",  # name contains "l2"; newest
        "mag/2025/mag_l2_burst.cdf",
    ]
    # Both names contain "burst" (newest first); the third matches only in its folder.
    assert _paths(_search(client, "burst l2")) == [
        "swe/2025/swe_l2_burst.cdf",
        "mag/2025/mag_l2_burst.cdf",
        "burst/mag_l2.cdf",
    ]
    assert _search(client, "mag swe")["total"] == 0


LITERALS = ["a%b.txt", "a_b.txt", "a*b.txt", "a?b.txt", "axb.txt", "a\\b.txt"]


@pytest.mark.req("API-16")
@pytest.mark.parametrize("path", LITERALS[:4] + LITERALS[5:])
def test_wildcard_characters_match_only_themselves(
    client: TestClient, ctx: AppContext, path: str
) -> None:
    with ctx.write_session() as session:
        for i, p in enumerate(LITERALS):
            add_file(session, "demo", f"lit/{p}", minute=i)
    word = path.removesuffix(".txt")
    assert _paths(_search(client, word)) == [f"lit/{path}"]


@pytest.mark.req("API-16")
def test_latest_only_and_include_deleted(client: TestClient, ctx: AppContext) -> None:
    with ctx.write_session() as session:
        add_file(session, "demo", "v/item_v1.cdf", latest=False, minute=1)
        add_file(session, "demo", "v/item_v2.cdf", minute=2)
        add_file(session, "demo", "v/item_v0.cdf", latest=False, deleted=True, minute=0)
    assert _paths(_search(client, "item")) == ["v/item_v2.cdf", "v/item_v1.cdf"]
    assert _paths(_search(client, "item", latest_only=True)) == ["v/item_v2.cdf"]
    body = _search(client, "item", include_deleted=True)
    assert _paths(body) == ["v/item_v2.cdf", "v/item_v1.cdf", "v/item_v0.cdf"]
    assert body["items"][2]["deleted_at"] is not None
    assert body["projects"] == {"demo": 3}


@pytest.mark.req("API-16")
def test_paging_totals_and_per_project_counts(client: TestClient, ctx: AppContext) -> None:
    with ctx.write_session() as session:
        for i in range(15):
            add_file(session, "demo", f"p/page_{i:02}.txt", minute=i)
        for i in range(10):
            add_file(session, "other", f"p/page_o{i:02}.txt", minute=100 + i)
        add_file(session, "third", "p/nothing.txt")
    pages = [_search(client, "page", limit=10, offset=o) for o in (0, 10, 20, 30)]
    assert [len(p["items"]) for p in pages] == [10, 10, 5, 0]
    for offset, page in zip((0, 10, 20, 30), pages, strict=True):
        assert page["total"] == 25
        assert (page["limit"], page["offset"]) == (10, offset)
        assert page["projects"] == {"demo": 15, "other": 10}
    seen = [path for page in pages for path in _paths(page)]
    assert len(set(seen)) == 25
    assert seen[:10] == [f"p/page_o{i:02}.txt" for i in range(9, -1, -1)]
    assert _search(client, "page")["limit"] == 20
    assert len(_search(client, "page", limit=100)["items"]) == 25


@pytest.mark.req("API-16")
@pytest.mark.parametrize(
    "params",
    [
        {},
        {"q": "a"},
        {"q": "   "},
        {"q": "x" * 201},
        {"q": "ab", "limit": 0},
        {"q": "ab", "limit": 101},
        {"q": "ab", "offset": -1},
        {"q": "ab", "latest_only": "maybe"},
    ],
)
def test_bad_parameters_give_422(client: TestClient, params: dict[str, Any]) -> None:
    assert client.get("/search/files", params=params).status_code == 422


@pytest.mark.req("API-16")
def test_limits_at_the_bounds_are_accepted(client: TestClient) -> None:
    assert client.get("/search/files", params={"q": "x" * 200, "limit": 100}).status_code == 200
    assert client.get("/search/files", params={"q": "ab", "limit": 1}).status_code == 200


@pytest.mark.req("API-16")
def test_items_carry_their_copies(client: TestClient, ctx: AppContext) -> None:
    with ctx.write_session() as session:
        add_file(session, "demo", "c/copy.txt", copies={"primary": FileState.AVAILABLE})
    (item,) = _search(client, "copy")["items"]
    assert item["project"] == "demo"
    assert [c["state"] for c in item["storage_locations"]] == ["available"]


@pytest.mark.req("API-16")
def test_model_needs_a_word_and_returns_nothing_without_projects(ctx: AppContext) -> None:
    with ctx.sessionmaker() as session:
        with pytest.raises(ValueError):
            models.search_files(session, ["demo"], [])
        assert models.search_files(session, [], ["x"]).total == 0
        assert models.search_files(session, ["no-such-project"], ["x"]).total == 0

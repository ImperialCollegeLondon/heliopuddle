"""Search (API-16) and project summaries (API-17) on a real Postgres.

The 200k-file dataset is inserted with set-based SQL (``generate_series``),
not through the API, so the setup takes seconds.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from heliopuddle.server.api import create_api
from heliopuddle.server.context import AppContext, set_context
from heliopuddle.server.database.migrate import upgrade_to_head
from heliopuddle.settings import Settings
from tests.golden_files import SUMMARY_EXPECTED, add_file, add_summary_dataset

PROJECT_YAML = """\
project: {name}
storage_locations:
{locations}
"""
LOCATION = "  - name: {name}\n    url: file://{root}\n"
PROJECTS = {"demo": ["primary", "secondary"], "other": ["main"], "third": ["main"]}
PER_PROJECT = 66_667  # 200,001 files in all
BUDGET_S = 0.5


def _config(root: Path) -> None:
    config = root / "config"
    config.mkdir(parents=True)
    for project, names in PROJECTS.items():
        locations = ""
        for name in names:
            store = root / "stores" / project / name
            store.mkdir(parents=True)
            locations += LOCATION.format(name=name, root=store)
        (config / f"{project}.yaml").write_text(
            PROJECT_YAML.format(name=project, locations=locations.rstrip("\n"))
        )


@pytest.fixture
def pg_ctx(postgres_url: str, tmp_path: Path) -> Iterator[AppContext]:
    """A migrated, config-synced header-mode context on a fresh Postgres database."""
    _config(tmp_path)
    settings = Settings(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        database_url=postgres_url,
        auth_mode="header",
    )
    ctx = AppContext.from_settings(settings)
    upgrade_to_head(ctx.engine)
    ctx.sync_config()
    set_context(ctx)
    yield ctx
    set_context(None)
    ctx.dispose()


@pytest.fixture
def client(pg_ctx: AppContext) -> Iterator[TestClient]:
    with TestClient(create_api(pg_ctx)) as c:
        yield c


BULK_SQL = [
    # One series per file.
    """
    INSERT INTO series (project_id, series_key)
    SELECT p.id, 'k' || g
    FROM project p CROSS JOIN generate_series(1, :n) AS g
    """,
    # Paths like inst3/2025/017/sample_l2_000123_v1.cdf; one file in 50 deleted.
    """
    INSERT INTO file (project_id, path, name, series_id, size, hash, hash_algo,
                      is_latest, ignored_from_all_rules, created_at, updated_at, deleted_at)
    SELECT s.project_id, d.dir || '/' || d.name, d.name, s.id, d.g, md5(s.id::text), 'md5',
           true, false, d.created, d.created,
           CASE WHEN d.g % 50 = 0 THEN d.created END
    FROM series s
    CROSS JOIN LATERAL (
        SELECT substr(s.series_key, 2)::bigint AS g
    ) AS k
    CROSS JOIN LATERAL (
        SELECT k.g AS g,
               'inst' || (k.g % 7) || '/2025/' || lpad((k.g % 365)::text, 3, '0') AS dir,
               'sample_l' || (k.g % 3) || '_' || lpad(k.g::text, 6, '0')
                   || '_v' || (k.g % 4) || '.cdf' AS name,
               timestamptz '2025-01-01' + k.g * interval '1 minute' AS created
    ) AS d
    """,
    # One copy each, in the project's first storage location.
    """
    INSERT INTO file_storage_location (file_id, storage_location_id, state, updated_at, arrived_at)
    SELECT f.id, (SELECT min(sl.id) FROM storage_location sl WHERE sl.project_id = f.project_id),
           CASE WHEN f.id % 10 = 0 THEN 'missing' ELSE 'available' END, now(), now()
    FROM file f
    """,
    "ANALYZE",
]


def _timed_search(client: TestClient, **params: Any) -> tuple[float, dict[str, Any]]:
    """The best of three timings, so a noisy shared CI runner does not decide the result."""
    best = float("inf")
    body: dict[str, Any] = {}
    for _ in range(3):
        start = time.perf_counter()
        r = client.get("/search/files", params=params)
        best = min(best, time.perf_counter() - start)
        assert r.status_code == 200, r.text
        body = r.json()
    return best, body


@pytest.mark.slow
@pytest.mark.req("API-16")
def test_search_over_200k_files_returns_a_page_within_500ms(
    pg_ctx: AppContext, client: TestClient
) -> None:
    with pg_ctx.engine.begin() as conn:
        for sql in BULK_SQL:
            conn.execute(text(sql), {"n": PER_PROJECT})
        assert conn.scalar(text("SELECT count(*) FROM file")) == 3 * PER_PROJECT

    _timed_search(client, q="sample")  # warm the connection pool and caches
    cases: list[tuple[dict[str, Any], int]] = [
        # Every live file matches: the worst case for ranking and counting.
        ({"q": "sample"}, 3 * (PER_PROJECT - PER_PROJECT // 50)),
        ({"q": "SAMPLE_L2 inst3 v1"}, -1),
        ({"q": "001234"}, 3),
        ({"q": "sample", "latest_only": True, "include_deleted": True, "offset": 5000}, -1),
        ({"q": "sample", "project": ["demo", "third"], "limit": 100}, -1),
        ({"q": "no-such-word"}, 0),
    ]
    timings: list[float] = []
    for params, expected_total in cases:
        elapsed, body = _timed_search(client, **params)
        timings.append(elapsed)
        assert elapsed < BUDGET_S, (params, elapsed)
        if expected_total >= 0:
            assert body["total"] == expected_total, params
        assert body["total"] == sum(body["projects"].values())
        if body["total"]:
            assert body["items"]
    print(f"search timings (s): {[round(t, 3) for t in timings]}")

    _, exact = _timed_search(client, q="sample_l1_001234_v2.cdf")
    assert [item["name"] for item in exact["items"]] == ["sample_l1_001234_v2.cdf"] * 3
    assert exact["projects"] == {"demo": 1, "other": 1, "third": 1}


@pytest.mark.req("API-16")
def test_search_matching_and_ranking_on_postgres(pg_ctx: AppContext, client: TestClient) -> None:
    with pg_ctx.write_session() as session:
        for i, path in enumerate(["a%b.txt", "a_b.txt", "a*b.txt", "a?b.txt", "axb.txt"]):
            add_file(session, "demo", f"lit/{path}", minute=i)
        add_file(session, "demo", "orbit/zzz.txt", minute=9)
        add_file(session, "demo", "a/my_orbit.cdf", minute=8)
        add_file(session, "other", "b/orbit_v2.cdf", minute=1)
        add_file(session, "demo", "d/ORBIT", minute=0)
    for word in ("a%b", "a_b", "a*b", "a?b"):
        _, body = _timed_search(client, q=word)
        assert [i["path"] for i in body["items"]] == [f"lit/{word}.txt"]
    _, body = _timed_search(client, q="orbit")
    assert [i["path"] for i in body["items"]] == [
        "d/ORBIT",
        "b/orbit_v2.cdf",
        "a/my_orbit.cdf",
        "orbit/zzz.txt",
    ]
    assert body["projects"] == {"demo": 3, "other": 1}


@pytest.mark.req("API-17")
def test_summary_on_postgres_matches_the_golden_dataset(
    pg_ctx: AppContext, client: TestClient
) -> None:
    with pg_ctx.write_session() as session:
        add_summary_dataset(session, other="other", other_location="main")
    r = client.get("/projects/demo/summary")
    assert r.status_code == 200, r.text
    assert r.json() == SUMMARY_EXPECTED
    third = client.get("/projects/third/summary").json()
    assert third["files"] == 0
    assert third["storage_locations"][0]["states"] == dict.fromkeys(
        SUMMARY_EXPECTED["storage_locations"][0]["states"], 0
    )


@pytest.mark.req("API-16")
def test_search_columns_are_stored_on_postgres(pg_ctx: AppContext) -> None:
    with pg_ctx.write_session() as session:
        add_file(session, "demo", "Data/Scan_A.CSV", minute=1)
    with pg_ctx.engine.connect() as conn:
        assert tuple(conn.execute(text("SELECT path_lower, name_lower FROM file")).one()) == (
            "data/scan_a.csv",
            "scan_a.csv",
        )
        generated = conn.execute(
            text(
                "SELECT column_name, is_generated FROM information_schema.columns "
                "WHERE table_name = 'file' AND column_name IN ('path_lower', 'name_lower')"
            )
        ).all()
    assert {(name, flag) for name, flag in generated} == {
        ("path_lower", "ALWAYS"),
        ("name_lower", "ALWAYS"),
    }
    with pg_ctx.engine.connect() as conn:
        assert conn.execute(text("SELECT name_lower FROM file")).scalar_one() == "scan_a.csv"

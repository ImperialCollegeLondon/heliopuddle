"""Index deployments: specs and builds."""

from __future__ import annotations

import logging
import types
from collections.abc import Mapping

import pytest

from heliopuddle.config import ProjectConfig
from heliopuddle.server.context import AppContext
from heliopuddle.server.services import deploy
from heliopuddle.server.services.deploy import (
    INDEX_TAG,
    build_deployments,
    index_deployment_specs,
    served_index_deployments,
    storage_location_ids,
)
from tests.unit._index_deploy_helpers import (  # noqa: F401
    _no_reconcile_deployments,
    _project,
    index_full,
    index_incremental,
    index_module,
)

_FIXTURES = (index_module,)  # referenced so ruff sees the imports used

pytestmark = pytest.mark.req("SVC-09")


def _ids(projects: Mapping[str, ProjectConfig]) -> dict[tuple[str, str], tuple[int, int]]:
    """Ids as config sync would give them: projects and storage locations in order."""
    ids: dict[tuple[str, str], tuple[int, int]] = {}
    sl_id = 0
    for pid, name in enumerate(projects, start=1):
        for loc in projects[name].storage_locations:
            sl_id += 1
            ids[(name, loc.name)] = (pid, sl_id)
    return ids


# --- specs ----------------------------------------------------------------------------


def test_specs_per_storage_location_with_schedules() -> None:
    projects = {
        "b": _project("b", ["one"]),
        "a": _project(
            "a", ["x", "y"], full_schedule="0 3 * * *", incremental_schedule="*/10 * * * *"
        ),
    }

    specs = index_deployment_specs(projects, _ids(projects))

    assert [(s.full_name, s.cron) for s in specs] == [
        ("index_full/index-full-p2-sl2-a-x", "0 3 * * *"),
        ("index_incremental/index-incremental-p2-sl2-a-x", "*/10 * * * *"),
        ("index_full/index-full-p2-sl3-a-y", "0 3 * * *"),
        ("index_incremental/index-incremental-p2-sl3-a-y", "*/10 * * * *"),
        ("index_full/index-full-p1-sl1-b-one", None),
        ("index_incremental/index-incremental-p1-sl1-b-one", None),
    ]
    assert specs[0].parameters == {"project": "a", "storage_location": "x"}
    assert "Full index of storage location x of project a" in specs[0].description
    assert "Incremental index" in specs[1].description


@pytest.mark.req("SVC-13")
def test_names_that_used_to_collide_are_distinct(index_module: types.ModuleType) -> None:
    projects = {
        "alpha-beta": _project("alpha-beta", ["primary"]),
        "alpha": _project("alpha", ["beta-primary"]),
    }

    deps = build_deployments(projects=projects, ids=_ids(projects))

    index_names = sorted(served_index_deployments(deps))
    assert index_names == [
        "index_full/index-full-p1-sl1-alpha-beta-primary",
        "index_full/index-full-p2-sl2-alpha-beta-primary",
        "index_incremental/index-incremental-p1-sl1-alpha-beta-primary",
        "index_incremental/index-incremental-p2-sl2-alpha-beta-primary",
    ]
    params = {d.name: d.parameters for d in deps if d.name.startswith("index-full")}
    assert params == {
        "index-full-p1-sl1-alpha-beta-primary": {
            "project": "alpha-beta",
            "storage_location": "primary",
        },
        "index-full-p2-sl2-alpha-beta-primary": {
            "project": "alpha",
            "storage_location": "beta-primary",
        },
    }


@pytest.mark.req("SVC-13")
def test_duplicate_identities_are_rejected_before_registration(
    index_module: types.ModuleType,
) -> None:
    projects = {"p": _project("p", ["a", "b"])}

    with pytest.raises(ValueError, match="collide"):
        build_deployments(projects=projects, ids={("p", "a"): (1, 7), ("p", "b"): (1, 7)})
    with pytest.raises(ValueError, match="no DB id"):
        build_deployments(projects=projects, ids={("p", "a"): (1, 1)})


def test_build_index_deployments(index_module: types.ModuleType) -> None:
    projects = {"p": _project("p", ["s1"], full_schedule="0 3 * * *")}

    deps = {
        f"{d.flow_name}/{d.name}": d
        for d in build_deployments(projects=projects, ids=_ids(projects))
    }

    assert set(deps) == {
        "verify_files/verify-files",
        "delete_files/delete-files",
        "run_rule_once/rule-once",
        "index_full/index-full-p1-sl1-p-s1",
        "index_incremental/index-incremental-p1-sl1-p-s1",
    }
    full = deps["index_full/index-full-p1-sl1-p-s1"]
    assert full.parameters == {"project": "p", "storage_location": "s1"}
    assert [s.schedule.cron for s in full.schedules or []] == ["0 3 * * *"]  # type: ignore[union-attr]
    assert full.tags == [INDEX_TAG]
    assert full.concurrency_limit == 1
    assert full.entrypoint is not None and full.entrypoint.endswith(".index_full")
    incremental = deps["index_incremental/index-incremental-p1-sl1-p-s1"]
    assert not incremental.schedules
    assert served_index_deployments(deps.values()) == {
        "index_full/index-full-p1-sl1-p-s1",
        "index_incremental/index-incremental-p1-sl1-p-s1",
    }


def test_build_without_index_module_serves_no_index_deployments(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(deploy, "INDEX_MODULE", "heliopuddle.server.services.no_index_here")
    projects = {"p": _project("p", ["s1"])}

    with caplog.at_level(logging.WARNING):
        deps = build_deployments(projects=projects, ids=_ids(projects))

    assert {d.name for d in deps} == {"verify-files", "delete-files", "rule-once"}
    assert "Not serving index deployments" in caplog.text


def test_build_without_projects_has_no_index_deployments(index_module: types.ModuleType) -> None:
    assert {d.name for d in build_deployments()} == {"verify-files", "delete-files", "rule-once"}


@pytest.mark.req("SVC-13")
def test_storage_location_ids_come_from_the_db(ctx: AppContext) -> None:
    assert storage_location_ids(ctx) == {("demo", "primary"): (1, 1), ("demo", "secondary"): (1, 2)}

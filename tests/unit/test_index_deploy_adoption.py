"""Index deployments: adoption decisions and submission."""

from __future__ import annotations

import logging
import types
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import IndexProgress, StorageLocation
from heliopuddle.server.services.deploy import (
    ADOPTION_TAG,
    IndexDeploymentSpec,
    adoption_key,
    build_deployments,
    choose_adoption_key,
    storage_location_ids,
    storage_locations_to_adopt,
    submit_adoption_runs,
)
from tests.unit._index_deploy_helpers import (  # noqa: F401
    _no_reconcile_deployments,
    index_full,
    index_incremental,
    index_module,
)

_FIXTURES = (index_module,)  # referenced so ruff sees the imports used

pytestmark = pytest.mark.req("SVC-09")


# --- adoption decisions ---------------------------------------------------------------


def _set_progress(ctx: AppContext, name: str, last_full_at: datetime | None) -> None:
    with ctx.write_session() as session:
        loc = session.query(StorageLocation).filter_by(name=name).one()
        session.merge(IndexProgress(storage_location_id=loc.id, last_full_at=last_full_at))


def _adopt(ctx: AppContext) -> list[tuple[str, str]]:
    return [(t.project, t.storage_location) for t in storage_locations_to_adopt(ctx)]


def test_adopt_storage_locations_never_fully_indexed(ctx: AppContext) -> None:
    assert storage_locations_to_adopt(ctx) == [
        ("demo", "primary", 1, 1),
        ("demo", "secondary", 1, 2),
    ]

    _set_progress(ctx, "primary", datetime.now(UTC))
    _set_progress(ctx, "secondary", None)  # only an incremental so far

    assert _adopt(ctx) == [("demo", "secondary")]


def test_inactive_storage_locations_are_not_adopted(ctx: AppContext) -> None:
    with ctx.write_session() as session:
        session.query(StorageLocation).filter_by(name="secondary").one().active = False

    assert _adopt(ctx) == [("demo", "primary")]


class _FakeClient:
    """Answers the adoption-key lookups from a list of earlier adoption runs."""

    def __init__(self, runs: list[tuple[str, str | None, str]] | None = None) -> None:
        # (deployment full name, idempotency key, state type)
        self.runs = runs or []

    @staticmethod
    def _id(name: str) -> uuid.UUID:
        return uuid.uuid5(uuid.NAMESPACE_URL, name)

    def read_deployment_by_name(self, name: str) -> Any:
        return types.SimpleNamespace(id=self._id(name))

    def read_flow_runs(self, **kwargs: Any) -> list[Any]:
        from prefect.client.schemas.objects import StateType

        assert kwargs["flow_run_filter"].tags.all_ == [ADOPTION_TAG]
        return [
            types.SimpleNamespace(
                deployment_id=self._id(dep), idempotency_key=key, state_type=StateType(state)
            )
            for dep, key, state in self.runs
        ]


PRIMARY = "index_full/index-full-p1-sl1-demo-primary"


SECONDARY = "index_full/index-full-p1-sl2-demo-secondary"


@pytest.mark.req("SVC-18")
@pytest.mark.parametrize(
    ("runs", "expected"),
    [
        ([], ("adopt-sl1-g1", False)),
        ([(PRIMARY, "adopt-sl1-g1", "FAILED")], ("adopt-sl1-g2", False)),
        (
            [(PRIMARY, "adopt-sl1-g3", "COMPLETED"), (PRIMARY, "adopt-sl1-g1", "FAILED")],
            ("adopt-sl1-g4", False),
        ),
        ([(PRIMARY, "adopt-sl1-g2", "SCHEDULED")], ("adopt-sl1-g2", True)),  # reused
        ([(PRIMARY, None, "RUNNING")], ("", True)),  # older run without a key: wait
        # another storage location's keys never count; another deployment's do
        ([(SECONDARY, "adopt-sl2-g5", "COMPLETED")], ("adopt-sl1-g1", False)),
        ([("index_full/old-name", "adopt-sl1-g4", "RUNNING")], ("adopt-sl1-g5", False)),
    ],
)
def test_choose_adoption_key(runs: list[Any], expected: tuple[str, bool]) -> None:
    assert choose_adoption_key(_FakeClient(runs), PRIMARY, 1) == expected  # type: ignore[arg-type]
    assert adoption_key(1, 2) == "adopt-sl1-g2"


@pytest.mark.req("SVC-13", "SVC-18")
def test_submit_adoption_runs_decisions(
    ctx: AppContext, index_module: types.ModuleType, caplog: pytest.LogCaptureFixture
) -> None:
    deps = build_deployments(projects=ctx.projects, ids=storage_location_ids(ctx))
    submitted: list[tuple[str, dict[str, Any]]] = []

    def run(name: str, **kwargs: Any) -> Any:
        submitted.append((name, kwargs))
        return types.SimpleNamespace(id=f"run-{len(submitted)}")

    ids = submit_adoption_runs(ctx, deps, _FakeClient(), run)  # type: ignore[arg-type]

    assert ids == ["run-1", "run-2"]
    assert [n for n, _ in submitted] == [PRIMARY, SECONDARY]
    assert submitted[0][1] == {
        "parameters": {"project": "demo", "storage_location": "primary"},
        "idempotency_key": "adopt-sl1-g1",
        "timeout": 0,
        "tags": [ADOPTION_TAG],
        "as_subflow": False,
    }
    assert submitted[1][1]["idempotency_key"] == "adopt-sl2-g1"

    # an unfinished adoption run is not submitted again (its key is reused)
    submitted.clear()
    client = _FakeClient([(PRIMARY, "adopt-sl1-g1", "PENDING")])
    assert submit_adoption_runs(ctx, deps, client, run) == ["run-2"]  # type: ignore[arg-type]
    assert [(n, k["idempotency_key"]) for n, k in submitted] == [
        (PRIMARY, "adopt-sl1-g1"),
        (SECONDARY, "adopt-sl2-g1"),
    ]

    # an unfinished run without a key: nothing submitted for it
    submitted.clear()
    client = _FakeClient([(PRIMARY, None, "RUNNING")])
    assert submit_adoption_runs(ctx, deps, client, run) == ["run-1"]  # type: ignore[arg-type]
    assert [n for n, _ in submitted] == [SECONDARY]

    # not served (no index module): nothing to submit
    assert submit_adoption_runs(ctx, build_deployments(), _FakeClient(), run) == []  # type: ignore[arg-type]

    # a failure is logged and the next storage location is still tried
    def flaky(name: str, **kwargs: Any) -> Any:
        if name.endswith("primary"):
            raise RuntimeError("no Prefect")
        return types.SimpleNamespace(id="ok")

    with caplog.at_level(logging.ERROR):
        assert submit_adoption_runs(ctx, deps, _FakeClient(), flaky) == ["ok"]  # type: ignore[arg-type]
    assert "Submitting the adoption run of demo/primary failed: RuntimeError: no Prefect" in (
        caplog.text
    )


def test_index_spec_is_hashable_and_named() -> None:
    spec = IndexDeploymentSpec("incremental", 3, 4, "p", "s", None)
    assert spec.flow == "index_incremental"
    assert spec.name == "index-incremental-p3-sl4-p-s"
    assert spec.identity == ("incremental", 3, 4)
    assert {spec} == {IndexDeploymentSpec("incremental", 3, 4, "p", "s", None)}

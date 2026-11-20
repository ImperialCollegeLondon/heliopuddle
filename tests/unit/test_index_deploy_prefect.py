"""Index deployments: concurrency limit races and a Prefect server."""

from __future__ import annotations

import threading
import types
import uuid
from typing import Any

import pytest

from heliopuddle.server.context import AppContext
from heliopuddle.server.services import deploy
from heliopuddle.server.services.deploy import (
    ADOPTION_TAG,
    ServicesRunner,
    build_deployments,
    prepare_index_services,
    storage_location_ids,
    submit_adoption_runs,
    upsert_limit,
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


def _unique(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:8]}"


# --- concurrency limits: creation races (SVC-18) -----------------------------------------


class _RacingLimits:
    """A client whose first create(s) lose a race against another process."""

    def __init__(self, conflicts: int, error: type[Exception] | None = None) -> None:
        self.conflicts = conflicts
        self.calls = 0
        self.error = error

    def upsert_global_concurrency_limit_by_name(self, name: str, limit: int) -> None:
        from prefect.exceptions import ObjectAlreadyExists

        self.calls += 1
        if self.error is not None:
            raise self.error("unrelated")
        if self.calls <= self.conflicts:
            raise ObjectAlreadyExists(http_exc=Exception("409"))


@pytest.mark.req("SVC-18")
def test_upsert_limit_rereads_after_losing_a_creation_race() -> None:
    client = _RacingLimits(conflicts=2)
    upsert_limit(client, "storage-location:p:s")  # type: ignore[arg-type]
    assert client.calls == 3

    always = _RacingLimits(conflicts=99)
    from prefect.exceptions import ObjectAlreadyExists

    with pytest.raises(ObjectAlreadyExists):
        upsert_limit(always, "storage-location:p:s")  # type: ignore[arg-type]
    assert always.calls == deploy.LIMIT_ATTEMPTS

    other = _RacingLimits(conflicts=0, error=OSError)
    with pytest.raises(OSError):
        upsert_limit(other, "storage-location:p:s")  # type: ignore[arg-type]
    assert other.calls == 1


@pytest.mark.req("SVC-18")
def test_http_409_counts_as_a_conflict() -> None:
    error = Exception("conflict")
    error.response = types.SimpleNamespace(status_code=409)  # type: ignore[attr-defined]
    assert deploy.is_conflict(error)  # pyright: ignore[reportPrivateUsage]
    assert not deploy.is_conflict(Exception("other"))  # pyright: ignore[reportPrivateUsage]


# --- against a Prefect server ---------------------------------------------------------


def _serve_once(deployments: list[Any]) -> None:
    """Register ``deployments`` with Prefect (nothing executes their runs)."""
    services = ServicesRunner(deployments, query_seconds=1)
    services.start()
    services.stop()


@pytest.mark.usefixtures("prefect_harness")
@pytest.mark.req("SVC-13")
@pytest.mark.slow
def test_prepare_creates_limits_and_deletes_stale_deployments(
    index_module: types.ModuleType,
) -> None:
    from prefect.client.orchestration import get_client

    project = _unique("p")
    old = {project: _project(project, ["keep", "gone"], full_schedule="0 3 * * *")}
    new = {project: _project(project, ["keep"])}  # "gone" removed, schedule removed
    ids = {(project, "keep"): (900, 901), (project, "gone"): (900, 902)}

    first = build_deployments(projects=old, ids=ids)
    # an index deployment in the older name format, from an earlier release
    legacy = index_full.to_deployment(name=f"index-full-{project}-keep")
    prepare_index_services(old, first)
    _serve_once([*first, legacy])
    keep_name = f"index_full/index-full-p900-sl901-{project}-keep"
    with get_client(sync_client=True) as client:
        keep = client.read_deployment_by_name(keep_name)
        assert [s.schedule.cron for s in keep.schedules] == ["0 3 * * *"]  # type: ignore[union-attr]
        assert keep.global_concurrency_limit is not None
        assert keep.global_concurrency_limit.limit == 1
        assert keep.parameters == {"project": project, "storage_location": "keep"}

    second = build_deployments(projects=new, ids=ids)
    deleted = prepare_index_services(new, second)
    _serve_once(second)

    assert {d for d in deleted if project in d} == {
        f"index_full/index-full-p900-sl902-{project}-gone",
        f"index_full/index-full-{project}-keep",
        f"index_incremental/index-incremental-p900-sl902-{project}-gone",
    }
    with get_client(sync_client=True) as client:
        names = {d.name for d in client.read_deployments()}
        assert f"index-full-p900-sl901-{project}-keep" in names
        assert not {n for n in names if project in n and "gone" in n}
        assert f"index-full-{project}-keep" not in names
        keep = client.read_deployment_by_name(keep_name)
        assert keep.schedules == []
        limit = client.read_global_concurrency_limit_by_name(f"storage-location:{project}:keep")
        assert limit.limit == 1
        assert limit.active


@pytest.mark.usefixtures("prefect_harness")
@pytest.mark.req("SVC-18")
def test_concurrent_prepares_both_succeed(index_module: types.ModuleType) -> None:
    from prefect.client.orchestration import get_client

    project = _unique("race")
    projects = {project: _project(project, ["a", "b", "c"])}
    stale = index_full.to_deployment(name=f"index-full-p0-sl0-{project}-stale")
    _serve_once([stale])
    with get_client(sync_client=True) as client:
        # Everything else registered so far counts as served here.
        served = [
            types.SimpleNamespace(flow_name=flow_name, name=d.name, schedules=d.schedules)
            for flow_name in ("index_full", "index_incremental")
            for d in client.read_deployments()
            if d.name != stale.name
        ]
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []
    deleted: list[list[str]] = []

    def start() -> None:
        try:
            barrier.wait()
            deleted.append(prepare_index_services(projects, served))  # type: ignore[arg-type]
        except BaseException as e:  # pragma: no cover - reported below
            errors.append(e)

    threads = [threading.Thread(target=start) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)

    assert errors == []
    assert f"index_full/{stale.name}" in {name for d in deleted for name in d}
    with get_client(sync_client=True) as client:
        assert stale.name not in {d.name for d in client.read_deployments()}
        for loc in ("a", "b", "c"):
            limit = client.read_global_concurrency_limit_by_name(
                f"storage-location:{project}:{loc}"
            )
            assert limit.limit == 1


@pytest.mark.usefixtures("prefect_harness")
@pytest.mark.req("SVC-18")
def test_adoption_run_is_submitted_once(ctx: AppContext, index_module: types.ModuleType) -> None:
    from prefect.client.orchestration import get_client

    deps = [
        d
        for d in build_deployments(projects=ctx.projects, ids=storage_location_ids(ctx))
        if d.name == "index-full-p1-sl1-demo-primary"
    ]
    _serve_once(deps)  # nothing executes the run: it stays scheduled

    first = submit_adoption_runs(ctx, deps)
    second = submit_adoption_runs(ctx, deps)

    assert len(first) == 1
    assert second == []
    with get_client(sync_client=True) as client:
        run = client.read_flow_run(uuid.UUID(first[0]))
    assert ADOPTION_TAG in run.tags
    assert run.parameters == {"project": "demo", "storage_location": "primary"}
    assert run.idempotency_key is not None and run.idempotency_key.startswith("adopt-sl1-g")


@pytest.mark.usefixtures("prefect_harness")
@pytest.mark.req("SVC-18")
def test_racing_starts_create_one_adoption_run(
    ctx: AppContext, index_module: types.ModuleType
) -> None:
    from prefect.client.orchestration import get_client
    from prefect.client.schemas.filters import DeploymentFilter, DeploymentFilterId

    # A storage location unique to this test, so earlier runs do not count.
    sl_name = _unique("race")
    ctx.projects["demo"].storage_locations.append(
        ctx.projects["demo"].storage_locations[0].model_copy(update={"name": sl_name})
    )
    ctx.sync_config()
    ids = storage_location_ids(ctx)
    deps = [
        d
        for d in build_deployments(projects=ctx.projects, ids=ids)
        if d.name.startswith("index-full-") and d.name.endswith(sl_name)
    ]
    _serve_once(deps)
    barrier = threading.Barrier(2)
    results: list[list[str]] = []

    def start() -> None:
        barrier.wait()
        results.append(submit_adoption_runs(ctx, deps))

    threads = [threading.Thread(target=start) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)

    with get_client(sync_client=True) as client:
        dep = client.read_deployment_by_name(f"index_full/{deps[0].name}")
        runs = client.read_flow_runs(
            deployment_filter=DeploymentFilter(id=DeploymentFilterId(any_=[dep.id]))
        )
    assert len(runs) == 1, [r.idempotency_key for r in runs]
    sl_id = ids[("demo", sl_name)][1]
    assert runs[0].idempotency_key == f"adopt-sl{sl_id}-g1"
    assert sorted(len(r) for r in results) in ([0, 1], [1, 1])  # same run id at most
    assert {i for r in results for i in r} == {str(runs[0].id)}

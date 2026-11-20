"""Adopt storage locations that were never fully indexed (SVC-10).

After serving, :func:`submit_adoption_runs` submits an ``index_full`` run for
each such storage location, with a Prefect idempotency key so that racing
starts create one run.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

from heliopuddle.server.services.deploy_prepare import served_index_deployments
from heliopuddle.server.services.deploy_state import storage_locations_to_adopt
from heliopuddle.server.services.index_names import index_deployment_full_name

if TYPE_CHECKING:
    from prefect.client.orchestration import SyncPrefectClient
    from prefect.client.schemas.objects import FlowRun
    from prefect.deployments.runner import RunnerDeployment

    from heliopuddle.server.context import AppContext

logger = logging.getLogger("heliopuddle.server.services.deploy")

ADOPTION_TAG = "heliopuddle-adoption"
"""Tag of the ``index_full`` runs submitted on start to adopt a storage location."""


_UNFINISHED_STATES = ("SCHEDULED", "PENDING", "RUNNING", "PAUSED", "CANCELLING")
ADOPTION_HISTORY_LIMIT = 200
"""Earlier adoption runs read to choose the next idempotency key."""


def adoption_key(storage_location_id: int, generation: int) -> str:
    """Return the idempotency key of an adoption run: ``adopt-sl<id>-g<generation>``."""
    return f"adopt-sl{storage_location_id}-g{generation}"


def _adoption_runs(client: SyncPrefectClient, flow_name: str) -> list[FlowRun]:
    """The adoption runs of ``flow_name`` (any deployment, any state), newest first."""
    from prefect.client.schemas.filters import (
        FlowFilter,
        FlowFilterName,
        FlowRunFilter,
        FlowRunFilterTags,
    )
    from prefect.client.schemas.sorting import FlowRunSort

    return list(
        client.read_flow_runs(
            flow_filter=FlowFilter(name=FlowFilterName(any_=[flow_name])),
            flow_run_filter=FlowRunFilter(tags=FlowRunFilterTags(all_=[ADOPTION_TAG])),
            sort=FlowRunSort.EXPECTED_START_TIME_DESC,
            limit=ADOPTION_HISTORY_LIMIT,
        )
    )


def choose_adoption_key(
    client: SyncPrefectClient, full_name: str, storage_location_id: int
) -> tuple[str, bool]:
    """Choose the idempotency key of the next adoption run of a storage location (SVC-18).

    The generation comes from the Prefect run history, so no DB counter is
    needed:

    - an unfinished adoption run of this deployment → its key again, so
      submitting is a no-op that returns that run;
    - otherwise one more than the highest generation among earlier adoption
      runs of that storage location (any deployment of the flow, so a reset
      database never reuses an old run's key), or 1.

    Every process starting at the same time computes the same key, so Prefect
    creates one run. After a failed adoption, or a storage URL change (which
    resets index progress), the next start computes a new key.

    Args:
        client: a Prefect client.
        full_name: ``index_full/<deployment>``.
        storage_location_id: the storage location's DB id.

    Returns:
        The key, and True if it is that of an unfinished run.
    """
    deployment = client.read_deployment_by_name(full_name)
    pattern = re.compile(rf"^adopt-sl{storage_location_id}-g(\d+)$")
    highest = 0
    for run in _adoption_runs(client, full_name.split("/", 1)[0]):
        match = pattern.match(run.idempotency_key or "")
        unfinished = run.state_type is not None and run.state_type.value in _UNFINISHED_STATES
        if run.deployment_id == deployment.id and unfinished:
            if match:
                return run.idempotency_key or "", True
            return "", True  # an older run without a key: wait for it
        if match:
            highest = max(highest, int(match.group(1)))
    return adoption_key(storage_location_id, highest + 1), False


def submit_adoption_runs(
    ctx: AppContext,
    deployments: Iterable[RunnerDeployment],
    client: SyncPrefectClient | None = None,
    run: Callable[..., Any] | None = None,
) -> list[str]:
    """Submit an ``index_full`` run for each storage location never fully indexed (D10).

    Called once the deployments are served. A storage location is skipped if
    its full index deployment is not served, or if an adoption run of it has
    not finished yet. Each run is submitted with explicit parameters and the
    idempotency key from :func:`choose_adoption_key`, so processes starting
    at the same time create one run (SVC-18). Failures are logged, not
    raised: the next start tries again.

    Args:
        ctx: the app context (its database is only read).
        deployments: the served deployments.
        client: a Prefect client (default: a new one for the current API).
        run: submits a run (default :func:`prefect.deployments.run_deployment`).

    Returns:
        The ids of the newly submitted flow runs.
    """
    if client is None:
        from prefect.client.orchestration import get_client

        with get_client(sync_client=True) as new_client:
            return submit_adoption_runs(ctx, deployments, new_client, run)
    if run is None:
        from prefect.deployments import run_deployment

        run = run_deployment

    served = served_index_deployments(deployments)
    submitted: list[str] = []
    for target in storage_locations_to_adopt(ctx):
        project, sl = target.project, target.storage_location
        full_name = index_deployment_full_name(
            "full", target.project_id, target.storage_location_id, project, sl
        )
        if full_name not in served:
            continue
        try:
            key, unfinished = choose_adoption_key(client, full_name, target.storage_location_id)
            if unfinished and not key:
                logger.info("Adoption of %s/%s is already submitted", project, sl)
                continue
            flow_run = run(
                full_name,
                parameters={"project": project, "storage_location": sl},
                idempotency_key=key,
                timeout=0,
                tags=[ADOPTION_TAG],
                as_subflow=False,
            )
        except Exception as e:
            logger.error(
                "Submitting the adoption run of %s/%s failed: %s: %s",
                project, sl, type(e).__name__, e,
            )  # fmt: skip
            continue
        if unfinished:
            logger.info("Adoption of %s/%s is already submitted (%s)", project, sl, key)
            continue
        logger.info(
            "Submitted %s (flow run %s, key %s) to adopt %s/%s",
            full_name, flow_run.id, key, project, sl,
        )  # fmt: skip
        submitted.append(str(flow_run.id))
    logger.info("Adoption runs submitted: %d", len(submitted))
    return submitted

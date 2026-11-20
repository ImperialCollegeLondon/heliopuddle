"""Prepare Prefect for the served deployments: limits and stale deployments (SVC-18).

Run before serving: creates the global concurrency limits the flows hold and
deletes deployments (and schedules) that are no longer in config.
"""

from __future__ import annotations

import contextlib
import importlib
import logging
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING

from heliopuddle.server.services.index_names import (
    INDEX_FLOW_NAMES,
    is_index_deployment_name,
    storage_location_lock,
)
from heliopuddle.server.services.rule_names import RECONCILE_FLOW_NAME

if TYPE_CHECKING:
    from prefect.client.orchestration import SyncPrefectClient
    from prefect.deployments.runner import RunnerDeployment

    from heliopuddle.config import ProjectConfig
    from heliopuddle.server.context import AppContext

logger = logging.getLogger("heliopuddle.server.services.deploy")

LIMIT_ATTEMPTS = 3
"""Tries to create or update a concurrency limit when racing another process."""


def is_index_deployment(deployment: RunnerDeployment) -> bool:
    return deployment.flow_name in set(INDEX_FLOW_NAMES.values())


def served_index_deployments(deployments: Iterable[RunnerDeployment]) -> set[str]:
    """Return the ``<flow>/<deployment>`` names of the index deployments in ``deployments``."""
    return {f"{d.flow_name}/{d.name}" for d in deployments if is_index_deployment(d)}


def is_conflict(error: Exception) -> bool:
    from prefect.exceptions import ObjectAlreadyExists

    if isinstance(error, ObjectAlreadyExists):
        return True
    response = getattr(error, "response", None)
    return getattr(response, "status_code", None) == 409


def upsert_limit(client: SyncPrefectClient, name: str, limit: int = 1) -> None:
    """Create the global concurrency limit ``name`` with ``limit`` slots, or set it (SVC-18).

    Another process starting at the same time may create it between the read
    and the create; that conflict is resolved by reading it again and
    updating it.

    Raises:
        Exception: the last error if every attempt conflicted, or any other
            Prefect error.
    """
    for attempt in range(1, LIMIT_ATTEMPTS + 1):
        try:
            client.upsert_global_concurrency_limit_by_name(name, limit)
            return
        except Exception as e:
            if not is_conflict(e) or attempt == LIMIT_ATTEMPTS:
                raise
            logger.debug("Concurrency limit %s was created meanwhile; reading it again", name)


def prepare_index_services(
    projects: Mapping[str, ProjectConfig],
    deployments: Iterable[RunnerDeployment],
    client: SyncPrefectClient | None = None,
) -> list[str]:
    """Get Prefect ready for the index deployments, before they are served.

    - Creates (or resets to 1) the global concurrency limit
      ``storage-location:<p>:<sl>`` of every storage location in config; the
      index flows hold it (D11). A creation race with another process is
      resolved by re-reading (SVC-18).
    - Deletes index deployments (flows ``index_full``/``index_incremental``
      with names ``index-<mode>-...``, current or older format) that are not
      among ``deployments``.
    - Removes the schedules of served index deployments that have none in
      config (re-registering a deployment keeps old schedules).

    Args:
        projects: loaded project configs.
        deployments: the deployments about to be served.
        client: a Prefect client (default: a new one for the current API).

    Returns:
        The ``<flow>/<deployment>`` names of the deleted deployments.
    """
    if client is None:
        from prefect.client.orchestration import get_client

        with get_client(sync_client=True) as new_client:
            return prepare_index_services(projects, deployments, new_client)

    from prefect.client.schemas.filters import FlowFilter, FlowFilterName
    from prefect.exceptions import ObjectNotFound

    for name in sorted(projects):
        for loc in projects[name].storage_locations:
            upsert_limit(client, storage_location_lock(name, loc.name), 1)

    deployments = list(deployments)
    served = served_index_deployments(deployments)
    unscheduled = {
        f"{d.flow_name}/{d.name}" for d in deployments if is_index_deployment(d) and not d.schedules
    }
    deleted: list[str] = []
    for mode, flow_name in INDEX_FLOW_NAMES.items():
        flow_filter = FlowFilter(name=FlowFilterName(any_=[flow_name]))
        for dep in client.read_deployments(flow_filter=flow_filter):
            if not is_index_deployment_name(mode, dep.name):
                continue
            full_name = f"{flow_name}/{dep.name}"
            if full_name not in served:
                logger.info("Deleting index deployment %s: not in config", full_name)
                with contextlib.suppress(ObjectNotFound):  # another start deleted it
                    client.delete_deployment(dep.id)
                deleted.append(full_name)
            elif full_name in unscheduled:
                for schedule in dep.schedules:
                    logger.info("Removing a schedule of %s: not in config", full_name)
                    with contextlib.suppress(ObjectNotFound):
                        client.delete_deployment_schedule(dep.id, schedule.id)
    return deleted


def prepare_rule_services(
    projects: Mapping[str, ProjectConfig],
    deployments: Iterable[RunnerDeployment],
    client: SyncPrefectClient | None = None,
) -> list[str]:
    """Create rule limits and remove stale rule deployments and obsolete schedules.

    Named schedules use a stable slug, so changing a cron updates the same
    schedule rather than accumulating duplicates across process restarts.
    """
    if client is None:
        from prefect.client.orchestration import get_client

        with get_client(sync_client=True) as new_client:
            return prepare_rule_services(projects, deployments, new_client)
    from prefect.client.schemas.filters import FlowFilter, FlowFilterName
    from prefect.exceptions import ObjectNotFound

    for project, cfg in sorted(projects.items()):
        for rule in cfg.rules:
            upsert_limit(client, f"rule:{project}:{rule.name}", 1)
    served = {dep.name: dep for dep in deployments if dep.flow_name == "run_rule"}
    deleted: list[str] = []
    for dep in client.read_deployments(
        flow_filter=FlowFilter(name=FlowFilterName(any_=["run_rule"]))
    ):
        if not dep.name.startswith("rule-"):
            continue
        desired = served.get(dep.name)
        if desired is None:
            with contextlib.suppress(ObjectNotFound):
                client.delete_deployment(dep.id)
            deleted.append(f"run_rule/{dep.name}")
            continue
        for schedule in dep.schedules:
            if not desired.schedules or schedule.slug != "rule":
                with contextlib.suppress(ObjectNotFound):
                    client.delete_deployment_schedule(dep.id, schedule.id)
    return deleted


def prepare_reconcile_services(
    deployments: Iterable[RunnerDeployment],
    client: SyncPrefectClient | None = None,
) -> list[str]:
    """Remove stale reconcile deployments and obsolete schedules before serving.

    Deployments of the ``reconcile`` flow named ``reconcile-...`` that are not
    among ``deployments`` are deleted; a served one without a cron in config
    loses its old schedules. Project-level serialization is the deployment's
    concurrency limit, so there are no global limits to create here.

    Returns:
        The ``<flow>/<deployment>`` names of the deleted deployments.
    """
    if client is None:
        from prefect.client.orchestration import get_client

        with get_client(sync_client=True) as new_client:
            return prepare_reconcile_services(deployments, new_client)
    from prefect.client.schemas.filters import FlowFilter, FlowFilterName
    from prefect.exceptions import ObjectNotFound

    served = {dep.name: dep for dep in deployments if dep.flow_name == RECONCILE_FLOW_NAME}
    deleted: list[str] = []
    for dep in client.read_deployments(
        flow_filter=FlowFilter(name=FlowFilterName(any_=[RECONCILE_FLOW_NAME]))
    ):
        if not dep.name.startswith("reconcile-"):
            continue
        desired = served.get(dep.name)
        if desired is None:
            logger.info("Deleting reconcile deployment %s: not in config", dep.name)
            with contextlib.suppress(ObjectNotFound):  # another start deleted it
                client.delete_deployment(dep.id)
            deleted.append(f"{RECONCILE_FLOW_NAME}/{dep.name}")
            continue
        for schedule in dep.schedules:
            if not desired.schedules or schedule.slug != "reconcile":
                with contextlib.suppress(ObjectNotFound):
                    client.delete_deployment_schedule(dep.id, schedule.id)
    return deleted


def reconcile_rule_services(ctx: AppContext) -> None:
    """Re-check persisted switches after registration and cancel disabled future runs."""
    module_name = "heliopuddle.server.services.rule_control"
    try:
        control = importlib.import_module(module_name)
    except ModuleNotFoundError as error:
        if error.name != module_name:
            raise
        return
    control.reconcile_rule_schedules(ctx)

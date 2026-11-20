"""Prefect run-history helpers and errors of the rule controls (SVC-24/SVC-25).

See docs/design/rules.md. Prefect owns run history; these functions read it
through a client the caller opens.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from prefect.client.schemas.filters import (
    DeploymentFilter,
    DeploymentFilterId,
    FlowRunFilter,
    FlowRunFilterState,
    FlowRunFilterStateType,
    FlowRunFilterTags,
)
from prefect.client.schemas.objects import StateType
from prefect.client.schemas.sorting import FlowRunSort
from prefect.settings import (
    PREFECT_API_REQUEST_TIMEOUT,
    PREFECT_CLIENT_MAX_RETRIES,
    temporary_settings,
)
from prefect.states import Cancelled

if TYPE_CHECKING:
    from prefect.client.orchestration import SyncPrefectClient
    from prefect.client.schemas.objects import FlowRun

_ACTIVE = [
    StateType.SCHEDULED,
    StateType.PENDING,
    StateType.RUNNING,
    StateType.PAUSED,
    StateType.CANCELLING,
]
# Runs that have not selected their work yet: a trigger needs nothing more.
_QUEUED = [StateType.SCHEDULED, StateType.PENDING]
_HISTORY_PAGE = 200
_TRIGGER_TAG = "heliopuddle-rule-trigger"


class RuleDisabledError(Exception):
    """A rule is disabled by YAML or its persisted runtime switch."""


class RuleBusyError(Exception):
    """A manual run was refused: the rule already has an unfinished run.

    That run (scheduled, pending or running) re-selects after every batch and
    continues while work remains, so a second run is not needed.

    Attributes:
        flow_run_id: the unfinished run.
    """

    def __init__(self, name: str, flow_run_id: str) -> None:
        super().__init__(f"rule {name!r} already has an unfinished run {flow_run_id}")
        self.flow_run_id = flow_run_id


class ReconcileBusyError(Exception):
    """A reconcile was refused: the project already has an unfinished reconcile run.

    Attributes:
        flow_run_id: the unfinished run.
    """

    def __init__(self, project: str, flow_run_id: str) -> None:
        super().__init__(
            f"project {project!r} already has an unfinished reconcile run {flow_run_id}"
        )
        self.flow_run_id = flow_run_id


def _future_cron_run(run: FlowRun, now: datetime) -> bool:
    """Whether ``run`` is a not-yet-due run that the Prefect scheduler made for the cron.

    The scheduler pre-creates future ``Scheduled`` runs of every scheduled
    deployment. They have selected no work and must not count as unfinished, or
    a cron would block manual runs and make triggers no-ops until it is due.
    Runs hp creates itself (``auto_scheduled`` false), due or overdue runs and
    ``AwaitingConcurrencySlot`` runs all still count.
    """
    if run.state_type != StateType.SCHEDULED or not getattr(run, "auto_scheduled", False):
        return False
    if getattr(run.state, "name", None) == "AwaitingConcurrencySlot":
        return False
    start = (
        getattr(run, "expected_start_time", None)
        or getattr(run, "next_scheduled_start_time", None)
        or getattr(getattr(run, "state", None), "timestamp", None)
    )
    return start is not None and start > now


def unfinished_run(
    client: SyncPrefectClient, deployment_id: UUID, types: list[StateType] | None = None
) -> FlowRun | None:
    """Return one unfinished run of a deployment, if any.

    Unfinished means scheduled, pending, running, paused or cancelling (or only
    the given ``types``), except future runs the Prefect scheduler created for
    the cron (see :func:`_future_cron_run`). Pages are read in order so that a
    cron's many pre-created runs cannot hide a real one.
    """
    now = datetime.now(UTC)
    offset = 0
    while True:
        page = client.read_flow_runs(
            deployment_filter=_deployment_filter(deployment_id),
            flow_run_filter=_state_filter(types or _ACTIVE),
            limit=_HISTORY_PAGE,
            offset=offset,
        )
        for run in page:
            if not _future_cron_run(run, now):
                return run
        if len(page) < _HISTORY_PAGE:
            return None
        offset += len(page)


@contextmanager
def _prefect_requests() -> Iterator[None]:
    # Two retries ride out Prefect's transient 503s (its database briefly busy)
    # while bounding how long SDK backoff (~2s, ~4s) can hold this rule's
    # runtime-state lock through a real Prefect outage.
    with temporary_settings({PREFECT_CLIENT_MAX_RETRIES: 2, PREFECT_API_REQUEST_TIMEOUT: 5.0}):
        yield


def _deployment_filter(deployment_id: UUID) -> DeploymentFilter:
    return DeploymentFilter(id=DeploymentFilterId(any_=[deployment_id]))


def _state_filter(types: list[StateType]) -> FlowRunFilter:
    return FlowRunFilter(state=FlowRunFilterState(type=FlowRunFilterStateType(any_=types)))


def _cancel_scheduled(client: SyncPrefectClient, deployment_id: UUID) -> None:
    scheduled: list[FlowRun] = []
    offset = 0
    while True:
        page = client.read_flow_runs(
            deployment_filter=_deployment_filter(deployment_id),
            flow_run_filter=_state_filter([StateType.SCHEDULED]),
            sort=FlowRunSort.ID_DESC,
            limit=_HISTORY_PAGE,
            offset=offset,
        )
        scheduled.extend(page)
        if len(page) < _HISTORY_PAGE:
            break
        offset += len(page)
    for run in scheduled:
        current = client.read_flow_run(run.id)
        if current.state is None or current.state.type != StateType.SCHEDULED:
            continue
        result = client.set_flow_run_state(
            run.id, Cancelled(message="Rule disabled in heliopuddle"), force=False
        )
        if result.state is not None and result.state.type in {
            StateType.CANCELLED,
            StateType.PENDING,
            StateType.RUNNING,
            StateType.COMPLETED,
            StateType.FAILED,
            StateType.CRASHED,
            StateType.CANCELLING,
            StateType.PAUSED,
        }:
            continue
        raise RuntimeError(f"Prefect refused cancellation of scheduled run {run.id}")


def _trigger_generation(client: SyncPrefectClient, deployment_id: UUID) -> tuple[str, int]:
    prefix = f"hp-trigger-{deployment_id}-g"
    generation, offset = 0, 0
    while True:
        page = client.read_flow_runs(
            deployment_filter=_deployment_filter(deployment_id),
            flow_run_filter=FlowRunFilter(tags=FlowRunFilterTags(all_=[_TRIGGER_TAG])),
            sort=FlowRunSort.ID_DESC,
            limit=_HISTORY_PAGE,
            offset=offset,
        )
        for run in page:
            key = run.idempotency_key or ""
            if key.startswith(prefix) and key[len(prefix) :].isdigit():
                generation = max(generation, int(key[len(prefix) :]))
        if len(page) < _HISTORY_PAGE:
            return prefix, generation + 1
        offset += len(page)

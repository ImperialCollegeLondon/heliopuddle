"""Build the services' Prefect deployments and serve them (SVC-01, SVC-09, SVC-13, SVC-18).

The deployments are served by a Prefect :class:`~prefect.runner.Runner` (what
``prefect.serve()`` uses) running in a background thread with its own event
loop, so no worker or work pool is needed and the main thread stays free for
uvicorn. Flow runs execute in child processes started by the runner; they
reach config and the database through the ``HELIOPUDDLE_*`` environment.

Index deployments (M3) are built per active storage location from the project
config and the DB ids: ``index_full/index-full-p<id>-sl<id>-<p>-<sl>`` and
``index_incremental/index-incremental-p<id>-sl<id>-<p>-<sl>``, with cron
schedules from the project's ``index:`` settings. Before serving,
:func:`prepare_index_services` creates the ``storage-location:<p>:<sl>``
global concurrency limits the index flows hold and deletes index deployments
no longer in config. After serving, :func:`submit_adoption_runs` submits an
``index_full`` run for each storage location that has never been fully
indexed, with a Prefect idempotency key so that racing starts create one run.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from heliopuddle.server.services.deploy_adopt import (
    ADOPTION_TAG,
    adoption_key,
    choose_adoption_key,
    submit_adoption_runs,
)
from heliopuddle.server.services.deploy_prepare import (
    LIMIT_ATTEMPTS,
    is_conflict,
    is_index_deployment,
    prepare_index_services,
    prepare_reconcile_services,
    prepare_rule_services,
    reconcile_rule_services,
    served_index_deployments,
    upsert_limit,
)
from heliopuddle.server.services.deploy_runner import (
    START_TIMEOUT_S,
    STOP_TIMEOUT_S,
    ServicesRunner,
)
from heliopuddle.server.services.deploy_state import (
    AdoptionTarget,
    rule_enablement,
    storage_location_ids,
    storage_locations_to_adopt,
)
from heliopuddle.server.services.index_names import (
    INDEX_FLOW_NAMES,
    IndexMode,
    index_deployment_name,
)
from heliopuddle.server.services.rule_names import (
    RULE_ONCE_DEPLOYMENT,
    reconcile_deployment_name,
    rule_deployment_name,
)

if TYPE_CHECKING:
    from prefect.deployments.runner import RunnerDeployment

    from heliopuddle.config import ProjectConfig

__all__ = [
    "ADOPTION_TAG",
    "LIMIT_ATTEMPTS",
    "START_TIMEOUT_S",
    "STOP_TIMEOUT_S",
    "AdoptionTarget",
    "ServicesRunner",
    "adoption_key",
    "choose_adoption_key",
    "is_conflict",
    "is_index_deployment",
    "prepare_index_services",
    "prepare_reconcile_services",
    "prepare_rule_services",
    "reconcile_rule_services",
    "rule_enablement",
    "served_index_deployments",
    "storage_location_ids",
    "storage_locations_to_adopt",
    "submit_adoption_runs",
    "upsert_limit",
]

logger = logging.getLogger(__name__)

VERIFY_FILES_DEPLOYMENT = "verify-files"
DELETE_FILES_DEPLOYMENT = "delete-files"
INDEX_MODULE = "heliopuddle.server.services.index"
RULE_MODULE = "heliopuddle.server.services.rules"
RECONCILE_MODULE = "heliopuddle.server.services.reconcile"
INDEX_TAG = "heliopuddle-index"

StorageLocationIds = Mapping[tuple[str, str], tuple[int, int]]
"""``(project, storage location)`` → ``(project id, storage location id)``."""


@dataclass(frozen=True)
class DeploymentSpec:
    """A flow served as a deployment.

    Attributes:
        module: the module that defines the flow.
        flow: the flow's attribute name in ``module``.
        name: the deployment name (served as ``{flow name}/{name}``).
        description: shown in the Prefect UI.
    """

    module: str
    flow: str
    name: str
    description: str


DEPLOYMENTS: tuple[DeploymentSpec, ...] = (
    DeploymentSpec(
        module="heliopuddle.server.services.verify",
        flow="verify_files",
        name=VERIFY_FILES_DEPLOYMENT,
        description="Re-verify copies in their stores (runs verify_file per copy).",
    ),
    DeploymentSpec(
        module="heliopuddle.server.services.delete",
        flow="delete_files",
        name=DELETE_FILES_DEPLOYMENT,
        description="Remove files from every store (runs delete_file per file).",
    ),
    DeploymentSpec(
        module=RULE_MODULE,
        flow="run_rule_once",
        name=RULE_ONCE_DEPLOYMENT.split("/")[1],
        description="Run one bounded batch of a validated, unsaved rule specification.",
    ),
)


def _load_flow(spec: DeploymentSpec) -> Any | None:
    """Import the flow of ``spec``, or return None if its module does not exist.

    Only a missing module is tolerated; an error raised while importing an
    existing module propagates.
    """
    try:
        module = importlib.import_module(spec.module)
    except ModuleNotFoundError as e:
        if e.name != spec.module:
            raise
        logger.debug("Not serving %s: module %s does not exist", spec.name, spec.module)
        return None
    return getattr(module, spec.flow)


@dataclass(frozen=True)
class IndexDeploymentSpec:
    """An index deployment for one storage location (SVC-09, SVC-13).

    Attributes:
        mode: ``full`` or ``incremental``.
        project_id: the project's DB id.
        storage_location_id: the storage location's DB id.
        project: the project name.
        storage_location: the storage location name.
        cron: the cron schedule, or None for on-demand only.
    """

    mode: IndexMode
    project_id: int
    storage_location_id: int
    project: str
    storage_location: str
    cron: str | None

    @property
    def identity(self) -> tuple[IndexMode, int, int]:
        """What makes the deployment unique: mode, project id and storage location id."""
        return (self.mode, self.project_id, self.storage_location_id)

    @property
    def flow(self) -> str:
        """The flow name (``index_full`` or ``index_incremental``)."""
        return INDEX_FLOW_NAMES[self.mode]

    @property
    def name(self) -> str:
        """The deployment name, e.g. ``index-full-p1-sl1-demo-primary``."""
        return index_deployment_name(
            self.mode,
            self.project_id,
            self.storage_location_id,
            self.project,
            self.storage_location,
        )

    @property
    def full_name(self) -> str:
        """``<flow>/<deployment>``, as used by ``run_deployment``."""
        return f"{self.flow}/{self.name}"

    @property
    def parameters(self) -> dict[str, str]:
        """The flow run parameters."""
        return {"project": self.project, "storage_location": self.storage_location}

    @property
    def description(self) -> str:
        """Shown in the Prefect UI."""
        what = "Full index" if self.mode == "full" else "Incremental index"
        return f"{what} of storage location {self.storage_location} of project {self.project}."


def index_deployment_specs(
    projects: Mapping[str, ProjectConfig], ids: StorageLocationIds
) -> list[IndexDeploymentSpec]:
    """Return the index deployments of every storage location in config.

    The storage locations in config are the active ones (config sync marks
    the others inactive), so each gets a full and an incremental deployment,
    scheduled from the project's ``index.full_schedule`` and
    ``index.incremental_schedule`` (None: on demand only).

    Args:
        projects: loaded project configs keyed by name.
        ids: the DB ids of the storage locations (:func:`storage_location_ids`).

    Raises:
        ValueError: if a storage location has no ids, or two specs share an
            identity (mode, project id, storage location id) or a name;
            nothing is registered then.
    """
    specs: list[IndexDeploymentSpec] = []
    for name in sorted(projects):
        cfg = projects[name]
        for loc in cfg.storage_locations:
            if (name, loc.name) not in ids:
                raise ValueError(
                    f"storage location {name}/{loc.name} has no DB id; sync the config first"
                )
            pid, slid = ids[(name, loc.name)]
            schedules: tuple[tuple[IndexMode, str | None], ...] = (
                ("full", cfg.index.full_schedule),
                ("incremental", cfg.index.incremental_schedule),
            )
            for mode, cron in schedules:
                specs.append(IndexDeploymentSpec(mode, pid, slid, name, loc.name, cron))
    _reject_duplicates(specs)
    return specs


def _reject_duplicates(specs: Iterable[IndexDeploymentSpec]) -> None:
    identities: dict[tuple[IndexMode, int, int], IndexDeploymentSpec] = {}
    names: dict[str, IndexDeploymentSpec] = {}
    for spec in specs:
        for key, seen in ((spec.identity, identities), (spec.full_name, names)):
            other = seen.get(key)  # type: ignore[call-overload]
            if other is not None:
                raise ValueError(
                    f"index deployments of {other.project}/{other.storage_location} and "
                    f"{spec.project}/{spec.storage_location} collide ({key}); "
                    "not registering either"
                )
            seen[key] = spec  # type: ignore[index]


def _load_index_flows() -> dict[str, Any] | None:
    """Return the index flows by flow name, or None if the index module does not exist."""
    flows: dict[str, Any] = {}
    for flow_name in INDEX_FLOW_NAMES.values():
        flow = _load_flow(DeploymentSpec(INDEX_MODULE, flow_name, flow_name, ""))
        if flow is None:
            return None
        flows[flow_name] = flow
    return flows


@dataclass(frozen=True)
class RuleDeploymentSpec:
    """A configured rule's immutable identity, schedule and effective enablement."""

    project_id: int
    project: str
    rule: str
    cron: str | None
    enabled: bool

    @property
    def name(self) -> str:
        """Return the collision-free deployment name."""
        return rule_deployment_name(self.project_id, self.project, self.rule)


def rule_deployment_specs(
    projects: Mapping[str, ProjectConfig],
    ids: StorageLocationIds,
    enabled: Mapping[tuple[int, str], bool] | None = None,
) -> list[RuleDeploymentSpec]:
    """Build rule specs, rejecting missing DB identities and duplicates before registration."""
    specs: list[RuleDeploymentSpec] = []
    identities: set[tuple[int, str]] = set()
    names: set[str] = set()
    for project, cfg in sorted(projects.items()):
        project_ids = {pid for (name, _), (pid, _) in ids.items() if name == project}
        if cfg.rules and len(project_ids) != 1:
            raise ValueError(f"project {project} has no unique DB id; sync the config first")
        if not cfg.rules:
            continue
        pid = project_ids.pop()
        for rule in cfg.rules:
            spec = RuleDeploymentSpec(
                pid,
                project,
                rule.name,
                rule.schedule,
                rule.enabled and (enabled or {}).get((pid, rule.name), True),
            )
            identity = (pid, rule.name)
            if identity in identities or spec.name in names:
                raise ValueError(f"rule deployments collide ({project}/{rule.name})")
            identities.add(identity)
            names.add(spec.name)
            specs.append(spec)
    return specs


@dataclass(frozen=True)
class ReconcileDeploymentSpec:
    """A project's reconcile deployment: identity, schedule and scheduled-run parameters."""

    project_id: int
    project: str
    cron: str | None
    verify: bool
    rules: list[str] | None

    @property
    def name(self) -> str:
        """Return the collision-free deployment name."""
        return reconcile_deployment_name(self.project_id, self.project)

    @property
    def parameters(self) -> dict[str, Any]:
        """Return the flow parameters of a run started from the deployment."""
        return {"project": self.project, "verify": self.verify, "rules": self.rules}


def reconcile_deployment_specs(
    projects: Mapping[str, ProjectConfig], ids: StorageLocationIds
) -> list[ReconcileDeploymentSpec]:
    """Build one reconcile spec per project, rejecting missing ids and duplicates."""
    specs: list[ReconcileDeploymentSpec] = []
    names: set[str] = set()
    for project, cfg in sorted(projects.items()):
        project_ids = {pid for (name, _), (pid, _) in ids.items() if name == project}
        if len(project_ids) != 1:
            raise ValueError(f"project {project} has no unique DB id; sync the config first")
        spec = ReconcileDeploymentSpec(
            project_ids.pop(),
            project,
            cfg.reconcile.schedule,
            cfg.reconcile.verify,
            cfg.reconcile.rules,
        )
        if spec.name in names:
            raise ValueError(f"reconcile deployments collide ({project})")
        names.add(spec.name)
        specs.append(spec)
    return specs


def build_deployments(
    specs: tuple[DeploymentSpec, ...] = DEPLOYMENTS,
    projects: Mapping[str, ProjectConfig] | None = None,
    ids: StorageLocationIds | None = None,
    rule_enabled: Mapping[tuple[int, str], bool] | None = None,
) -> list[RunnerDeployment]:
    """Return the deployments served by ``server start``.

    ``verify_files/verify-files`` and ``delete_files/delete-files``; a spec
    whose module is not part of the installed package is skipped. With
    ``projects``, also the index deployments of every storage location
    (:func:`index_deployment_specs`), each limited to one active run at a
    time. Entrypoints are module paths, so they resolve from an installed
    package.

    Args:
        specs: the fixed deployments to build.
        projects: loaded project configs; None builds no index deployments.
        ids: the storage locations' DB ids (required with ``projects``).
        rule_enabled: effective runtime switches, read by :func:`rule_enablement`.

    Raises:
        ValueError: if index deployments would collide or lack ids.
    """
    from prefect.client.schemas.actions import DeploymentScheduleCreate
    from prefect.client.schemas.objects import ConcurrencyLimitConfig, ConcurrencyLimitStrategy
    from prefect.client.schemas.schedules import CronSchedule
    from prefect.types.entrypoint import EntrypointType

    index_specs = index_deployment_specs(projects, ids or {}) if projects else []
    rule_specs = rule_deployment_specs(projects, ids or {}, rule_enabled) if projects else []
    reconcile_specs = reconcile_deployment_specs(projects, ids or {}) if projects else []
    deployments: list[RunnerDeployment] = []
    for spec in specs:
        flow = _load_flow(spec)
        if flow is None:
            continue
        deployment = flow.to_deployment(
            name=spec.name,
            description=spec.description,
            entrypoint_type=EntrypointType.MODULE_PATH,
        )
        deployments.append(cast("RunnerDeployment", deployment))  # sync call: no coroutine
    flows = _load_index_flows() if index_specs else {}
    if flows is None and index_specs:
        logger.warning("Not serving index deployments: module %s does not exist", INDEX_MODULE)
    for ispec in index_specs if flows is not None else []:
        assert flows is not None
        deployment = flows[ispec.flow].to_deployment(
            name=ispec.name,
            description=ispec.description,
            cron=ispec.cron,
            parameters=ispec.parameters,
            tags=[INDEX_TAG],
            concurrency_limit=1,
            entrypoint_type=EntrypointType.MODULE_PATH,
        )
        deployments.append(cast("RunnerDeployment", deployment))
    if rule_specs:
        rule_flow = _load_flow(DeploymentSpec(RULE_MODULE, "run_rule", "run_rule", ""))
        if rule_flow is None:
            raise RuntimeError(f"configured rules require {RULE_MODULE}")
        for rspec in rule_specs:
            schedules = (
                [
                    DeploymentScheduleCreate(
                        schedule=CronSchedule(cron=rspec.cron), active=rspec.enabled, slug="rule"
                    )
                ]
                if rspec.cron
                else []
            )
            deployment = rule_flow.to_deployment(
                name=rspec.name,
                description=f"Rule {rspec.rule} of project {rspec.project}.",
                parameters={"project": rspec.project, "rule": rspec.rule},
                schedules=schedules,
                paused=not rspec.enabled,
                tags=["heliopuddle-rule"],
                # ENQUEUE: a continuation or queued follow-up waits for the
                # running run to finish instead of being cancelled.
                concurrency_limit=ConcurrencyLimitConfig(
                    limit=1, collision_strategy=ConcurrencyLimitStrategy.ENQUEUE
                ),
                entrypoint_type=EntrypointType.MODULE_PATH,
            )
            deployments.append(cast("RunnerDeployment", deployment))
    reconcile_flow = (
        _load_flow(DeploymentSpec(RECONCILE_MODULE, "reconcile", "reconcile", ""))
        if reconcile_specs
        else None
    )
    if reconcile_flow is None and reconcile_specs:
        logger.warning(
            "Not serving reconcile deployments: module %s does not exist", RECONCILE_MODULE
        )
    for cspec in reconcile_specs:
        if reconcile_flow is None:
            break
        schedules = (
            [
                DeploymentScheduleCreate(
                    schedule=CronSchedule(cron=cspec.cron), active=True, slug="reconcile"
                )
            ]
            if cspec.cron
            else []
        )
        deployment = reconcile_flow.to_deployment(
            name=cspec.name,
            description=f"Reconcile project {cspec.project}: index, verify, then rules.",
            parameters=cspec.parameters,
            schedules=schedules,
            tags=["heliopuddle-reconcile"],
            concurrency_limit=ConcurrencyLimitConfig(
                limit=1, collision_strategy=ConcurrencyLimitStrategy.ENQUEUE
            ),
            entrypoint_type=EntrypointType.MODULE_PATH,
        )
        deployments.append(cast("RunnerDeployment", deployment))
    return deployments

"""Rule runtime controls and best-effort notifications (SVC-24/SVC-25).

Prefect owns run history and idempotency; hp stores only the runtime switch and
selection progress. No local queue or run journal is involved.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal
from uuid import UUID

from prefect.client.orchestration import get_client
from prefect.settings import (
    PREFECT_API_URL,
)
from prefect.states import Scheduled
from sqlalchemy import select

from heliopuddle.server import models
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import Project, RuleState
from heliopuddle.server.models.rule_state import lock_rule_state
from heliopuddle.server.schemas import RuleRead, RuleTriggerResult, RunInfo
from heliopuddle.server.services import runs
from heliopuddle.server.services.rule_lookup import (
    _configured,
    _enabled,
    _project_id,
    _read,
    _snapshot,
)
from heliopuddle.server.services.rule_notify import notify_file_changes
from heliopuddle.server.services.rule_prefect import (
    _QUEUED,
    _TRIGGER_TAG,
    ReconcileBusyError,
    RuleBusyError,
    RuleDisabledError,
    _cancel_scheduled,
    _prefect_requests,
    _trigger_generation,
    unfinished_run,
)
from heliopuddle.server.services.run_logging import run_logger
from heliopuddle.settings import prefect_ui_url

if TYPE_CHECKING:
    pass

logger = run_logger(__name__)


def list_rules(ctx: AppContext, project: str) -> list[RuleRead]:
    """Return configured rules with defaults, effective switches and progress."""
    cfg = ctx.projects.get(project)
    if cfg is None:
        raise models.NotFoundError(f"project {project!r} not found")
    with ctx.sessionmaker() as session:
        project_id = _project_id(session, project)
        states = {
            state.rule_name: state
            for state in session.scalars(
                select(RuleState).where(RuleState.project_id == project_id)
            )
        }
        result = [_read(rule, project, project_id, states.get(rule.name)) for rule in cfg.rules]
    _deployment_links(ctx, result)
    return result


def get_rule(ctx: AppContext, project: str, name: str) -> RuleRead:
    """Return one configured rule without requiring Prefect to be reachable."""
    _, _, result = _snapshot(ctx, project, name)
    _deployment_links(ctx, [result])
    return result


def _set_deployment_link(ctx: AppContext, rule: RuleRead, deployment_id: UUID) -> None:
    rule.deployment_id = str(deployment_id)
    base = prefect_ui_url(ctx.settings)
    rule.deployment_url = f"{base}/deployments/deployment/{deployment_id}" if base else None


def _deployment_links(ctx: AppContext, rules: list[RuleRead]) -> None:
    if not rules or not PREFECT_API_URL.value():
        return
    try:
        with _prefect_requests(), get_client(sync_client=True) as client:
            for rule in rules:
                try:
                    deployment = client.read_deployment_by_name(rule.deployment)
                    _set_deployment_link(ctx, rule, deployment.id)
                except Exception:
                    logger.warning(
                        "Could not resolve Prefect deployment %s", rule.deployment, exc_info=True
                    )
    except Exception:
        logger.warning("Could not resolve Prefect deployment links", exc_info=True)


def submit_rule_run(
    ctx: AppContext,
    project: str,
    name: str,
    mode: Literal["full", "incremental"] = "incremental",
    dry_run: bool = False,
) -> RunInfo:
    """Submit a configured run while serialized against runtime disable and triggers.

    Raises:
        RuleDisabledError: the rule is disabled.
        RuleBusyError: the rule already has an unfinished run (none is submitted).
        RunSubmitError: Prefect could not be reached or refused the run.
    """
    cfg, project_id, current = _snapshot(ctx, project, name, require_enabled=True)
    try:
        with _prefect_requests(), get_client(sync_client=True) as client:
            deployment = client.read_deployment_by_name(current.deployment)
            with ctx.write_session() as session:
                state = lock_rule_state(session, project_id, name)
                _enabled(cfg, state, project)
                active = unfinished_run(client, deployment.id)
                if active:
                    raise RuleBusyError(name, str(active.id))
                return runs.submit_rule_run(
                    project, name, mode, dry_run, project_id=project_id, settings=ctx.settings
                )
    except (RuleDisabledError, RuleBusyError, runs.RunSubmitError):
        raise
    except Exception as error:
        raise runs.RunSubmitError(
            current.deployment,
            f"could not submit rule {name!r} in Prefect: {type(error).__name__}: {error}",
        ) from error


def submit_reconcile_run(
    ctx: AppContext, project: str, verify: bool = False, rules: list[str] | None = None
) -> RunInfo:
    """Submit a project's reconcile unless one is unfinished (API-04, SVC-27).

    Raises:
        models.NotFoundError: unknown project.
        ValueError: ``rules`` names a rule the project does not configure (callers
            such as the API check first to answer 422).
        ReconcileBusyError: the project already has an unfinished reconcile run.
        RunSubmitError: the deployment is missing or Prefect could not be reached.
    """
    from heliopuddle.server.services.rule_names import reconcile_deployment_full_name

    cfg = ctx.projects.get(project)
    if cfg is None:
        raise models.NotFoundError(f"project {project!r} not found")
    cfg.check_rule_names(rules)
    with ctx.sessionmaker() as session:
        project_id = _project_id(session, project)
    deployment_name = reconcile_deployment_full_name(project_id, project)
    try:
        with _prefect_requests(), get_client(sync_client=True) as client:
            deployment = client.read_deployment_by_name(deployment_name)
            # The project row lock serializes check-and-submit across processes,
            # so racing submissions get one run and a 409, never two runs.
            with ctx.write_session() as session:
                session.get_one(Project, project_id, with_for_update=True)
                active = unfinished_run(client, deployment.id)
                if active:
                    raise ReconcileBusyError(project, str(active.id))
                return runs.submit_reconcile_run(
                    project, verify, rules, project_id=project_id, settings=ctx.settings
                )
    except (ReconcileBusyError, runs.RunSubmitError):
        raise
    except Exception as error:
        raise runs.RunSubmitError(
            deployment_name,
            f"could not submit reconcile of {project!r} in Prefect: "
            f"{type(error).__name__}: {error}",
        ) from error


def _sync_prefect_state(ctx: AppContext, project: str, name: str) -> RuleRead:
    cfg, project_id, current = _snapshot(ctx, project, name)
    try:
        with _prefect_requests(), get_client(sync_client=True) as client:
            deployment = client.read_deployment_by_name(current.deployment)
            with ctx.write_session() as session:
                state = lock_rule_state(session, project_id, name)
                current = _read(cfg, project, project_id, state)
                _set_deployment_link(ctx, current, deployment.id)
                client.set_deployment_paused_state(deployment.id, not current.effective_enabled)
                for schedule in client.read_deployment_schedules(deployment.id):
                    client.update_deployment_schedule(
                        deployment.id, schedule.id, active=current.effective_enabled
                    )
                if not current.effective_enabled:
                    _cancel_scheduled(client, deployment.id)
    except Exception as error:
        raise runs.RunSubmitError(
            current.deployment,
            f"could not update rule {name!r} in Prefect: {type(error).__name__}: {error}",
        ) from error
    return current


def set_rule_enabled(ctx: AppContext, project: str, name: str, enabled: bool) -> RuleRead:
    """Persist the runtime switch, then reconcile schedules and future runs.

    A failed Prefect request is reported, not hidden. In particular a disabled
    switch stays committed, so active batches cannot schedule successors even
    while Prefect is unavailable. Repeating the operation reconciles it.
    """
    _configured(ctx, project, name)
    with ctx.write_session() as session:
        project_id = _project_id(session, project)
        state = lock_rule_state(session, project_id, name, create=True)
        assert state is not None
        state.enabled = enabled
    return _sync_prefect_state(ctx, project, name)


def reconcile_rule_schedules(ctx: AppContext) -> None:
    """Reapply persisted switches after deployments are registered at startup.

    Raises:
        RunSubmitError: Prefect refused schedule reconciliation; startup must fail.
    """
    for project, cfg in ctx.projects.items():
        for rule in cfg.rules:
            _sync_prefect_state(ctx, project, rule.name)


def trigger_rule(ctx: AppContext, project: str, name: str) -> RuleTriggerResult:
    """Make sure an incremental run will see the change, without stacking runs.

    - A scheduled or pending run (it has not selected its work yet): no-op.
    - Only a running (or pausing/cancelling) run: queue exactly one follow-up,
      keyed to that run, which waits for the deployment's slot (``ENQUEUE``).
      The running run may already have made its last check, so the follow-up
      picks up the change instead of it waiting for the next schedule.
    - No unfinished run: start one.

    A short rule-state lock serializes trigger/manual submissions with disable.
    Prefect's idempotency key comes from its own history and deployment identity,
    surviving process restarts and ambiguous submission failures. Deployment
    concurrency is a second guard, not a way to create-and-cancel duplicates.
    """
    cfg, project_id, current = _snapshot(ctx, project, name, require_enabled=True)
    try:
        with _prefect_requests(), get_client(sync_client=True) as client:
            deployment = client.read_deployment_by_name(current.deployment)
            with ctx.write_session() as session:
                state = lock_rule_state(session, project_id, name)
                _enabled(cfg, state, project)
                queued = unfinished_run(client, deployment.id, _QUEUED)
                if queued:
                    return RuleTriggerResult(started=False, flow_run_id=str(queued.id))
                active = unfinished_run(client, deployment.id)
                if active:
                    key = f"hp-trigger-{deployment.id}-after-{active.id}"
                else:
                    prefix, generation = _trigger_generation(client, deployment.id)
                    key = f"{prefix}{generation}"
                flow_run = client.create_flow_run_from_deployment(
                    deployment.id,
                    parameters={
                        "project": project,
                        "rule": name,
                        "mode": "incremental",
                        "dry_run": False,
                    },
                    state=Scheduled(),
                    tags=[_TRIGGER_TAG],
                    idempotency_key=key,
                )
                return RuleTriggerResult(started=True, flow_run_id=str(flow_run.id))
    except RuleDisabledError:
        raise
    except Exception as error:
        raise runs.RunSubmitError(
            current.deployment,
            f"could not trigger rule {name!r} in Prefect: {type(error).__name__}: {error}",
        ) from error


__all__ = [
    "ReconcileBusyError",
    "RuleBusyError",
    "RuleDisabledError",
    "get_rule",
    "list_rules",
    "notify_file_changes",
    "reconcile_rule_schedules",
    "set_rule_enabled",
    "submit_reconcile_run",
    "submit_rule_run",
    "trigger_rule",
    "unfinished_run",
]

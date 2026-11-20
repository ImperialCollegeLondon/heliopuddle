"""Submit index and rule runs to Prefect without waiting (API-04/API-10).

The runs endpoint resolves and validates the storage location, then calls
:func:`submit_index_run`, which creates a flow run of the storage location's
index deployment (named by :mod:`heliopuddle.server.services.index_names`)
and returns at once.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, cast

from prefect.deployments import run_deployment
from prefect.exceptions import ObjectNotFound

from heliopuddle.config.models import RuleSpec
from heliopuddle.server.schemas import RunInfo
from heliopuddle.server.services.index_names import IndexMode, index_deployment_full_name
from heliopuddle.settings import Settings, get_settings, prefect_ui_url

if TYPE_CHECKING:
    from prefect.client.schemas.objects import FlowRun


class RunSubmitError(Exception):
    """A run could not be submitted: the deployment is missing or Prefect failed.

    Attributes:
        deployment: the ``<flow>/<deployment>`` name that was submitted.
    """

    def __init__(self, deployment: str, message: str) -> None:
        super().__init__(message)
        self.deployment = deployment


def flow_run_url(flow_run_id: str, settings: Settings | None = None) -> str | None:
    """Return the Prefect UI link to a flow run, or None if the UI URL is unknown.

    Args:
        flow_run_id: the flow run id.
        settings: process settings; defaults to :func:`get_settings`.

    Returns:
        ``<prefect ui>/runs/flow-run/<id>``.
    """
    base = prefect_ui_url(settings or get_settings())
    return f"{base}/runs/flow-run/{flow_run_id}" if base else None


def submit_index_run(
    project: str,
    storage_location: str,
    mode: IndexMode,
    *,
    project_id: int,
    storage_location_id: int,
    settings: Settings | None = None,
) -> RunInfo:
    """Submit a run of a storage location's index deployment and return at once.

    The deployment is named from the ids and the names (SVC-13), and the run
    gets explicit ``project`` and ``storage_location`` parameters rather than
    relying on the deployment's defaults.

    Args:
        project: project name.
        storage_location: storage location name (already validated by the caller).
        mode: ``full`` or ``incremental``.
        project_id: the project's DB id.
        storage_location_id: the storage location's DB id.
        settings: process settings, for the Prefect UI link.

    Returns:
        The flow run id, its Prefect UI URL (None if unknown) and the
        deployment name.

    Raises:
        RunSubmitError: the deployment does not exist (it is registered when
            the services start), or Prefect could not be reached.
    """
    deployment = index_deployment_full_name(
        mode, project_id, storage_location_id, project, storage_location
    )
    parameters = {"project": project, "storage_location": storage_location}
    return _submit(deployment, parameters, settings)


def submit_rule_run(
    project: str,
    rule: str,
    mode: Literal["full", "incremental"] = "incremental",
    dry_run: bool = False,
    *,
    project_id: int,
    settings: Settings | None = None,
) -> RunInfo:
    """Submit an already-enabled configured rule without configuration overrides."""
    from heliopuddle.server.services.rule_names import rule_deployment_full_name

    return _submit(
        rule_deployment_full_name(project_id, project, rule),
        {"project": project, "rule": rule, "mode": mode, "dry_run": dry_run},
        settings,
    )


def submit_reconcile_run(
    project: str,
    verify: bool = False,
    rules: list[str] | None = None,
    *,
    project_id: int,
    settings: Settings | None = None,
) -> RunInfo:
    """Submit a run of a project's reconcile deployment with explicit parameters."""
    from heliopuddle.server.services.rule_names import reconcile_deployment_full_name

    return _submit(
        reconcile_deployment_full_name(project_id, project),
        {"project": project, "verify": verify, "rules": rules},
        settings,
    )


def submit_rule_once(project: str, spec: RuleSpec, *, settings: Settings | None = None) -> RunInfo:
    """Submit a validated, complete unsaved spec to the single-batch deployment."""
    from heliopuddle.server.services.rule_names import RULE_ONCE_DEPLOYMENT

    return _submit(
        RULE_ONCE_DEPLOYMENT,
        {"project": project, "spec": spec.model_dump(mode="json")},
        settings,
    )


def _submit(deployment: str, parameters: dict[str, Any], settings: Settings | None) -> RunInfo:
    try:
        # ``_sync=True``: always the blocking variant, even if called from a thread
        # that has an event loop.
        flow_run = cast(
            "FlowRun",
            run_deployment(
                deployment,
                parameters=parameters,
                timeout=0,
                as_subflow=False,
                _sync=True,  # type: ignore[call-arg]
            ),
        )
    except ObjectNotFound:
        raise RunSubmitError(
            deployment,
            f"deployment {deployment!r} not found; deployments are registered "
            "when the heliopuddle services start",
        ) from None
    except Exception as e:  # Prefect unreachable or misconfigured
        raise RunSubmitError(
            deployment, f"could not submit {deployment!r} to Prefect: {type(e).__name__}: {e}"
        ) from e
    run_id = str(flow_run.id)
    return RunInfo(
        flow_run_id=run_id, flow_run_url=flow_run_url(run_id, settings), deployment=deployment
    )

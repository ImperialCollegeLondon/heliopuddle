"""Submit background index, configured-rule and unsaved-rule runs (API-04/API-10)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from heliopuddle.server import models
from heliopuddle.server.api.deps import Ctx, require_project, requires
from heliopuddle.server.api.errors import rule_errors
from heliopuddle.server.auth.roles import ROLE_OPERATOR
from heliopuddle.server.schemas import RunInfo, RunSubmission
from heliopuddle.server.services import rule_control, runs

router = APIRouter(tags=["runs"])


@router.post(
    "/projects/{project}/runs",
    dependencies=[Depends(requires(ROLE_OPERATOR))],
    status_code=202,
    response_model=RunInfo,
    responses={
        404: {"description": "Unknown project, storage location or configured rule"},
        409: {
            "description": "The storage location is inactive, the rule is disabled, "
            "or a reconcile is already unfinished"
        },
        422: {
            "description": "Invalid one-off specification, unsupported run overrides "
            "or unknown reconcile rule names"
        },
        503: {"description": "The deployment is missing or Prefect is unreachable"},
    },
)
def submit_run(project: str, body: RunSubmission, ctx: Ctx) -> RunInfo:
    """Submit a validated run without waiting for it.

    Index requests retain their first-storage-location/full defaults.
    Configured rules accept only mode and dry run; one-offs validate complete
    unsaved specs, including this project's storage-location references.
    """
    cfg = require_project(ctx, project)
    if body.kind == "reconcile":
        try:
            cfg.check_rule_names(body.rules, field="rules")
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        with rule_errors():
            return rule_control.submit_reconcile_run(ctx, project, body.verify, body.rules)
    if body.kind == "rule":
        with rule_errors():
            return rule_control.submit_rule_run(ctx, project, body.rule, body.mode, body.dry_run)
    if body.kind == "rule_once":
        try:
            cfg.validate_rule(body.spec)
            with ctx.sessionmaker() as session:
                for name in (body.spec.source, body.spec.destination):
                    if name is None:
                        continue
                    location = models.get_storage_location(session, project, name)
                    if location is None or not location.active:
                        raise ValueError(f"storage location {name!r} is not active in {project!r}")
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        with rule_errors():
            return runs.submit_rule_once(project, body.spec, settings=ctx.settings)
    name = body.storage_location or cfg.storage_locations[0].name
    with ctx.sessionmaker() as session:
        row = models.get_storage_location(session, project, name)
        if row is None:
            raise HTTPException(
                status_code=404, detail=f"storage location {name!r} not found in {project!r}"
            )
        if not row.active:
            raise HTTPException(
                status_code=409, detail=f"storage location {name!r} in {project!r} is inactive"
            )
        project_id, storage_location_id = row.project_id, row.id
    try:
        return runs.submit_index_run(
            project,
            name,
            body.mode,
            project_id=project_id,
            storage_location_id=storage_location_id,
            settings=ctx.settings,
        )
    except runs.RunSubmitError as e:
        raise HTTPException(status_code=503, detail=str(e)) from None

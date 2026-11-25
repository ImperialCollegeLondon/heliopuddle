"""Rule selection windows and Prefect-held checkpoints (M4).

See docs/design/rules.md. A paused chain is recovered from the parameters of its
Prefect flow runs; hp keeps no queue or run table.
"""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from typing import Any

from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import RuleState, utcnow
from heliopuddle.server.models import rules as rule_db
from heliopuddle.server.schemas.rules import RuleCursor
from heliopuddle.server.services.rule_names import rule_deployment_full_name
from heliopuddle.server.services.rule_types import RuleContinuation, RuleMode, _enabled, _Rule


def _maximum(left: RuleCursor | None, right: RuleCursor | None) -> RuleCursor | None:
    if left is None:
        return right
    if right is None:
        return left
    return max((left, right), key=lambda cursor: (cursor.updated_at, cursor.id))


def _load_checkpoint(
    rule: _Rule, mode: RuleMode, watermark: RuleCursor | None, last_success_at: datetime | None
) -> RuleContinuation | None:
    """Recover a paused chain from Prefect parameters, without an hp queue/run table."""
    from prefect.client.orchestration import get_client
    from prefect.client.schemas.filters import FlowRunFilter, FlowRunFilterTags
    from prefect.client.schemas.sorting import FlowRunSort
    from prefect.context import FlowRunContext

    context = FlowRunContext.get()
    if context is None or context.flow_run is None:
        return None
    with get_client(sync_client=True) as client:
        runs = client.read_flow_runs(
            flow_run_filter=FlowRunFilter(
                tags=FlowRunFilterTags(all_=[_checkpoint_tag(rule, mode)])
            ),
            sort=FlowRunSort.START_TIME_DESC,
            limit=100,
        )
    for run in runs:
        if run.id == context.flow_run.id or run.parameters.get("mode", "incremental") != mode:
            continue
        data = run.parameters.get("continuation")
        if not data:
            continue
        checkpoint = RuleContinuation.model_validate(data)
        if (
            checkpoint.pending
            and checkpoint.watermark == watermark
            and checkpoint.state_success_at == last_success_at
            and checkpoint.spec_hash == sha256(rule.spec.model_dump_json().encode()).hexdigest()
        ):
            return checkpoint
        return None
    return None


def _checkpoint_tag(rule: _Rule, mode: RuleMode) -> str:
    return f"hp-rule-checkpoint-p{rule.project_id}-{rule.name}-{mode}"


def _persist_checkpoint(rule: _Rule, checkpoint: RuleContinuation, *, dry_run: bool) -> None:
    """Keep resume state on the owning Prefect run, including retrigger-disabled runs."""
    if dry_run:
        return
    from prefect.client.orchestration import get_client
    from prefect.context import FlowRunContext

    context = FlowRunContext.get()
    if context is None or context.flow_run is None:
        return
    with get_client(sync_client=True) as client:
        client.update_flow_run(
            context.flow_run.id,
            tags=sorted({*context.flow_run.tags, _checkpoint_tag(rule, checkpoint.mode)}),
            parameters={
                "project": rule.project,
                "rule": rule.name,
                "mode": checkpoint.mode,
                "dry_run": False,
                "continuation": checkpoint.model_dump(mode="json"),
            },
        )


def _schedule_successor(ctx: AppContext, rule: _Rule, *, parameters: dict[str, Any]) -> None:
    from prefect.client.orchestration import get_client
    from prefect.states import Cancelled, Scheduled

    if rule.name is None or not _enabled(ctx, rule):
        return
    data = parameters["continuation"]
    key = sha256(repr(data).encode()).hexdigest()
    with get_client(sync_client=True) as client:
        deployment = client.read_deployment_by_name(
            rule_deployment_full_name(rule.project_id, rule.project, rule.name)
        )
        if not _enabled(ctx, rule):
            return
        run = client.create_flow_run_from_deployment(
            deployment.id,
            parameters=parameters,
            state=Scheduled(scheduled_time=utcnow() + rule.spec.options.pause_between_runs),
            idempotency_key=f"rule-p{rule.project_id}-{rule.name}-{key}",
        )
        # Disable persists first, then cancels futures. Re-check after create
        # so a disable between its cancellation query and our submission wins.
        if not _enabled(ctx, rule):
            client.set_flow_run_state(
                run.id,
                Cancelled(message="Rule disabled while scheduling its continuation"),
                force=True,
            )


def _new_checkpoint(ctx: AppContext, rule: _Rule, mode: RuleMode) -> RuleContinuation:
    watermark, age_since = None, None
    if rule.name is not None:
        with ctx.sessionmaker() as session:
            state = session.get(RuleState, (rule.project_id, rule.name))
            if state is not None:
                age_since = state.last_success_at
                if state.watermark_at is not None and state.watermark_id is not None:
                    watermark = RuleCursor(updated_at=state.watermark_at, id=state.watermark_id)
        previous = _load_checkpoint(rule, mode, watermark, age_since)
        if previous is not None:
            return previous
    return RuleContinuation(
        mode=mode,
        selected_at=utcnow(),
        full_after_id=0 if mode == "full" or watermark is None else None,
        changed_after=watermark if mode == "incremental" else None,
        age_since=age_since,
        state_success_at=age_since,
        watermark=watermark,
        spec_hash=sha256(rule.spec.model_dump_json().encode()).hexdigest(),
    )


def _candidates(
    ctx: AppContext, rule: _Rule, checkpoint: RuleContinuation, limit: int
) -> list[rule_db.RuleCandidate]:
    with ctx.sessionmaker() as session:
        return rule_db.select_candidates(
            session,
            rule.project_id,
            rule.source_id,
            rule.spec.match,
            now=checkpoint.selected_at,
            after=checkpoint.cursor,
            changed_after=checkpoint.changed_after if checkpoint.full_after_id is None else None,
            age_since=checkpoint.age_since,
            after_id=checkpoint.full_after_id,
            through=checkpoint.selected_at if checkpoint.full_after_id is None else None,
            limit=limit,
            within=rule.within,
        )


def _remaining(
    ctx: AppContext, rule: _Rule, checkpoint: RuleContinuation
) -> RuleContinuation | None:
    if _candidates(ctx, rule, checkpoint, 1):
        return checkpoint
    if checkpoint.full_after_id is not None:
        return None
    # Re-query after the bounded snapshot. Fresh updates are a new window,
    # not an empty continuation of the exhausted one.
    # Changes tied with the old window's end may have committed after its query,
    # with any id, so the new window restarts at that timestamp (reruns are no-ops).
    lower = RuleCursor(updated_at=checkpoint.selected_at, id=0)
    with ctx.sessionmaker() as session:
        late = rule_db.select_candidates(
            session,
            rule.project_id,
            rule.source_id,
            rule.spec.match,
            now=utcnow(),
            changed_after=lower,
            age_since=checkpoint.selected_at,
            limit=1,
            within=rule.within,
        )
    if not late:
        return None
    return checkpoint.model_copy(
        update={
            "selected_at": max(utcnow(), late[0].cursor.updated_at),
            "changed_after": lower,
            "age_since": checkpoint.selected_at,
            "cursor": None,
        }
    )

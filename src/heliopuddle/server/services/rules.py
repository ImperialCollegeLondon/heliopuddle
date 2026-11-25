"""Bounded rule flows, immutable selection windows and Prefect continuations (M4)."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from time import sleep
from typing import Any

from prefect import flow, get_run_logger

from heliopuddle.config.models import RuleSpec
from heliopuddle.server import models
from heliopuddle.server.context import AppContext, get_context
from heliopuddle.server.database.orm import RuleState, utcnow
from heliopuddle.server.models import rules as rule_db
from heliopuddle.server.schemas.rules import RuleCursor
from heliopuddle.server.services.batches import BatchBudget
from heliopuddle.server.services.concurrency import ensure_concurrency_limit, hp_concurrency
from heliopuddle.server.services.rule_checkpoint import (
    _candidates,
    _load_checkpoint,
    _maximum,
    _new_checkpoint,
    _persist_checkpoint,
    _remaining,
    _schedule_successor,
    rule_spec_hash,
)
from heliopuddle.server.services.rule_types import (
    SYSTEM_USER,
    RuleContinuation,
    RuleMode,
    RuleSummary,
    _enabled,
    _resolve,
    _Rule,
)


def _execute_file(candidate: rule_db.RuleCandidate, spec: RuleSpec) -> Any:
    """Call the owned per-file task; importing lazily keeps service registration independent."""
    transfer = importlib.import_module("heliopuddle.server.services.transfer")
    if spec.action == "delete":
        return transfer.delete_source_copy(
            candidate.file_id,
            spec.source,
            expected_generation=candidate.generation,
            expected_hash=candidate.hash,
            user=SYSTEM_USER,
        )
    return transfer.transfer_file(
        candidate.file_id,
        spec.source,
        spec.destination,
        action=spec.action,
        options=spec.options,
        user=SYSTEM_USER,
    )


FileFailure = Callable[[rule_db.RuleCandidate, Exception], None]


def _process(
    ctx: AppContext,
    rule: _Rule,
    checkpoint: RuleContinuation,
    *,
    dry_run: bool,
    on_failure: FileFailure | None = None,
) -> tuple[RuleSummary, bool]:
    """Run one bounded batch of files, advancing ``checkpoint`` past each processed file.

    A failed file is logged and counted as ``failed``. With ``on_failure`` it
    is also reported there (which may raise to stop the batch) and the batch
    is not marked failed, so the caller decides; otherwise the batch is.

    Returns:
        The batch summary and whether any file failed without ``on_failure``.
    """
    summary = RuleSummary(project=rule.project, rule=rule.name, mode=checkpoint.mode)
    budget = BatchBudget(rule.spec.options.batch)
    logger = get_run_logger()
    failed = False
    while not budget.exhausted:
        page = _candidates(
            ctx,
            rule,
            checkpoint,
            min(100, max(1, budget.limits.max_files - budget.files))
            if budget.limits.max_files is not None
            else 100,
        )
        if not page:
            break
        for candidate in page:
            detail = None
            with ctx.sessionmaker() as session:
                eligible = rule_db.is_live_unignored(session, candidate.file_id)
            if not eligible:
                logger.info(
                    "rule %s: file=%s path=%s deleted or ignored since selected; skipped",
                    rule.name or "one-off",
                    candidate.file_id,
                    candidate.path,
                )
                checkpoint.cursor = candidate.cursor
                checkpoint.watermark = _maximum(checkpoint.watermark, candidate.cursor)
                if checkpoint.full_after_id is not None:
                    checkpoint.full_after_id = candidate.file_id
                continue
            try:
                if dry_run:
                    outcome = f"would_{rule.spec.action}"
                else:
                    result = _execute_file(candidate, rule.spec)
                    outcome, detail = result.outcome, result.detail
            except Exception as error:
                logger.exception("rule %s: %s failed", rule.name or "one-off", candidate.path)
                outcome = "failed"
                if on_failure is None:
                    failed = True
                else:
                    on_failure(candidate, error)
            logger.info(
                "rule %s: file=%s path=%s outcome=%s detail=%s",
                rule.name or "one-off",
                candidate.file_id,
                candidate.path,
                outcome,
                detail or "",
            )
            summary.processed += 1
            summary.bytes_processed += candidate.size
            summary.outcomes[outcome] = summary.outcomes.get(outcome, 0) + 1
            budget.files += 1
            budget.bytes += candidate.size
            checkpoint.cursor = candidate.cursor
            checkpoint.watermark = _maximum(checkpoint.watermark, candidate.cursor)
            if checkpoint.full_after_id is not None:
                checkpoint.full_after_id = candidate.file_id
            if budget.exhausted:
                break
    logger.info(
        "rule %s: processed=%d bytes=%d outcomes=%s",
        rule.name or "one-off",
        summary.processed,
        summary.bytes_processed,
        summary.outcomes,
    )
    return summary, failed


def _batch(
    ctx: AppContext, rule: _Rule, checkpoint: RuleContinuation, *, dry_run: bool
) -> RuleSummary:
    summary, failed = _process(ctx, rule, checkpoint, dry_run=dry_run)
    if failed:
        raise RuntimeError(f"rule {rule.name or 'one-off'} failed: {summary.outcomes}")
    next_checkpoint = _remaining(ctx, rule, checkpoint) if rule.name is not None else None
    summary.remaining = next_checkpoint is not None
    if rule.name is not None and not dry_run:
        checkpoint = next_checkpoint or checkpoint.model_copy(update={"pending": False})
        _persist_checkpoint(rule, checkpoint, dry_run=False)
        with ctx.write_session() as session:
            rule_db.record_progress(
                session,
                rule.project_id,
                rule.name,
                checkpoint.watermark,
                completed_at=checkpoint.selected_at if next_checkpoint is None else None,
            )
        if next_checkpoint is not None and rule.spec.options.retrigger and _enabled(ctx, rule):
            _schedule_successor(
                ctx,
                rule,
                parameters={
                    "project": rule.project,
                    "rule": rule.name,
                    "mode": checkpoint.mode,
                    "dry_run": False,
                    "continuation": checkpoint.model_dump(mode="json"),
                },
            )
    return summary


def reconcile_rule(
    project: str, rule: str, *, on_failure: FileFailure | None = None
) -> list[RuleSummary]:
    """Run one configured rule over the whole index inside the calling flow (reconcile).

    Full mode, batch by batch with ``options.pause_between_runs`` between
    batches, holding the rule's concurrency limit. Unlike :func:`run_rule` it
    never schedules a successor, never stores a checkpoint on the calling
    flow run and never records rule progress, so a file that failed is never
    passed by the rule's incremental watermark. A disabled rule does nothing.
    See docs/design/rules.md#reconcile-m5.

    Args:
        project: project name.
        rule: configured rule name.
        on_failure: called with each failed file; it may raise to stop the rule.

    Returns:
        One summary per batch; a single ``disabled`` summary for a disabled rule.

    Raises:
        NotFoundError: if the rule is not in config.
        RuntimeError: if a file failed and no ``on_failure`` was given.
    """
    ctx = get_context()
    try:
        config = ctx.projects[project].rule(rule)
    except KeyError:
        raise models.NotFoundError(f"rule {project}/{rule} is not in config") from None
    resolved = _resolve(ctx, project, config, rule)
    disabled = [RuleSummary(project=project, rule=rule, mode="full", disabled=True)]
    if not _enabled(ctx, resolved):
        return disabled
    lock = f"rule:{project}:{rule}"
    ensure_concurrency_limit(lock)
    summaries: list[RuleSummary] = []
    checkpoint = RuleContinuation(
        mode="full",
        selected_at=utcnow(),
        full_after_id=0,
        spec_hash=rule_spec_hash(resolved),
    )
    pause = config.options.pause_between_runs.total_seconds()
    while True:
        # Hold the rule's slot per batch, not across pauses, so scheduled and
        # triggered runs interleave; re-check enablement before every batch so
        # a disable during a pause stops the chain.
        with hp_concurrency(lock):
            if not _enabled(ctx, resolved):
                return summaries or disabled
            summary, failed = _process(
                ctx, resolved, checkpoint, dry_run=config.options.dry_run, on_failure=on_failure
            )
            if failed:
                raise RuntimeError(f"rule {rule} failed: {summary.outcomes}")
            summary.remaining = bool(_candidates(ctx, resolved, checkpoint, 1))
            summaries.append(summary)
        if not summary.remaining:
            return summaries
        if pause:
            sleep(pause)


@flow(name="run_rule")
def run_rule(
    project: str,
    rule: str,
    mode: RuleMode = "incremental",
    dry_run: bool = False,
    *,
    continuation: RuleContinuation | None = None,
) -> RuleSummary:
    """Run one configured-rule batch, preserving progress and chaining remaining work.

    Public callers supply only project/rule, mode and dry_run. Continuation
    is internal state owned by the flow; config is never overridden.
    Disabled rules do no work, including direct flow invocations.
    """
    ctx = get_context()
    try:
        config = ctx.projects[project].rule(rule)
    except KeyError:
        raise models.NotFoundError(f"rule {project}/{rule} is not in config") from None
    resolved = _resolve(ctx, project, config, rule)
    summary = RuleSummary(project=project, rule=rule, mode=mode, disabled=True)
    if not _enabled(ctx, resolved):
        return summary
    lock = f"rule:{project}:{rule}"
    ensure_concurrency_limit(lock)
    with hp_concurrency(lock):
        if not _enabled(ctx, resolved):
            return summary
        checkpoint = (
            RuleContinuation.model_validate(continuation)
            if continuation is not None
            else _new_checkpoint(ctx, resolved, mode)
        )
        if continuation is not None:
            with ctx.sessionmaker() as session:
                state = session.get(RuleState, (resolved.project_id, rule))
                watermark = (
                    RuleCursor(updated_at=state.watermark_at, id=state.watermark_id)
                    if state is not None
                    and state.watermark_at is not None
                    and state.watermark_id is not None
                    else None
                )
                last_success = state.last_success_at if state is not None else None
            if (
                not checkpoint.pending
                or checkpoint.state_success_at != last_success
                or checkpoint.spec_hash != rule_spec_hash(resolved)
            ):
                get_run_logger().info("rule %s: stale continuation skipped", rule)
                return RuleSummary(project=project, rule=rule, mode=mode)
            saved = _load_checkpoint(resolved, mode, watermark, last_success)
            if saved is not None:
                checkpoint = saved
            elif checkpoint.watermark != watermark:
                get_run_logger().info("rule %s: superseded continuation skipped", rule)
                return RuleSummary(project=project, rule=rule, mode=mode)
        return _batch(ctx, resolved, checkpoint, dry_run=dry_run or config.options.dry_run)


@flow(name="run_rule_once")
def run_rule_once(project: str, spec: RuleSpec) -> RuleSummary:
    """Run exactly one full batch of an unsaved spec, without progress writes or retrigger."""
    ctx = get_context()
    spec = RuleSpec.model_validate(spec)
    resolved = _resolve(ctx, project, spec)
    return _batch(
        ctx,
        resolved,
        RuleContinuation(mode="full", selected_at=utcnow(), full_after_id=0),
        dry_run=spec.options.dry_run,
    )

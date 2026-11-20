"""Full reconciliation of a project: index, optional verify, rules to a fixed point (M5).

See docs/design/rules.md#reconcile-m5. Everything runs inline inside one flow
run; nothing is queued, stored or published beyond the run log.
"""

from __future__ import annotations

from collections.abc import Callable

from prefect import flow, get_run_logger

from heliopuddle.server import models
from heliopuddle.server.context import get_context
from heliopuddle.server.models import index as index_db
from heliopuddle.server.models import rules as rule_db
from heliopuddle.server.models.rules import RuleCandidate
from heliopuddle.server.schemas import FileState
from heliopuddle.server.schemas.reconcile import (
    ReconcileFailure,
    ReconcilePass,
    ReconcileSummary,
)
from heliopuddle.server.services.concurrency import ensure_concurrency_limit, hp_concurrency
from heliopuddle.server.services.index import IndexCounts, index_full_chain
from heliopuddle.server.services.index_names import storage_location_lock
from heliopuddle.server.services.rules import reconcile_rule
from heliopuddle.server.services.verify import verify_file

VERIFY_BATCH = 100
"""Copies re-hashed per hold of a storage location's limit."""

INDEX_CHANGE_FIELDS = ("added", "changed", "removed", "missing", "unknown", "swept", "repaired")
"""Index counts that mean the DB or a store changed (not listed, skipped or reverified)."""

RULE_CHANGE_OUTCOMES = ("copied", "moved", "deleted", "conflict")
"""Rule outcomes that changed something; skipped, unchanged and would_* did not."""


def _index_changes(project: str, storage_location: str) -> int:
    total = IndexCounts()
    try:
        for batch in index_full_chain(project, storage_location):
            total.add(batch)
    except Exception as error:
        raise RuntimeError(
            f"reconcile index of {project}/{storage_location} failed: {error}"
        ) from error
    return sum(getattr(total, name) for name in INDEX_CHANGE_FIELDS)


def _verify(project: str, storage_location: str, storage_location_id: int) -> int:
    """Re-hash every available copy of a storage location; return those now unknown."""
    ctx = get_context()
    lock = storage_location_lock(project, storage_location)
    ensure_concurrency_limit(lock)
    unknown = 0
    after = 0
    while True:
        with ctx.sessionmaker() as session:
            ids = index_db.available_copy_ids(
                session, storage_location_id, after_id=after, limit=VERIFY_BATCH
            )
        if not ids:
            return unknown
        with hp_concurrency(lock):
            for copy_id in ids:
                if verify_file(copy_id) == FileState.UNKNOWN:
                    unknown += 1
        after = ids[-1]


class _TooManyFailures(RuntimeError):
    """Raised when failed files reach ``reconcile.max_failures``; already lists them all."""


def _describe(failures: list[ReconcileFailure]) -> str:
    return "; ".join(f"{f.rule}: {f.path}: {f.error}" for f in failures)


def _recorder(
    project: str, rule: str, result: ReconcilePass, max_failures: int
) -> Callable[[RuleCandidate, Exception], None]:
    def record(candidate: RuleCandidate, error: Exception) -> None:
        result.failures.append(ReconcileFailure(rule=rule, path=candidate.path, error=f"{error}"))
        if len(result.failures) >= max_failures:
            raise _TooManyFailures(
                f"reconcile of {project} stopped after {len(result.failures)} failed "
                f"files: {_describe(result.failures)}"
            )

    return record


def _rule_pass(project: str, rules: list[str], number: int, max_failures: int) -> ReconcilePass:
    result = ReconcilePass(number=number)
    for rule in rules:
        changes = 0
        try:
            for batch in reconcile_rule(
                project, rule, on_failure=_recorder(project, rule, result, max_failures)
            ):
                changes += sum(batch.outcomes.get(name, 0) for name in RULE_CHANGE_OUTCOMES)
        except _TooManyFailures:
            raise
        except Exception as error:
            message = f"reconcile of {project}: rule {rule} failed: {error}"
            if result.failures:
                message += (
                    f"; {len(result.failures)} file(s) already failed in pass {number}: "
                    f"{_describe(result.failures)}"
                )
            raise RuntimeError(message) from error
        result.changes[rule] = changes
    return result


def _rule_phase(
    project: str, summary: ReconcileSummary, max_passes: int, max_failures: int
) -> None:
    log = get_run_logger()
    if not summary.rules:
        return
    for number in range(1, max_passes + 1):
        result = _rule_pass(project, summary.rules, number, max_failures)
        summary.passes.append(result)
        log.info(
            "reconcile %s: pass %d changes=%s failures=%d",
            project,
            number,
            result.changes,
            len(result.failures),
        )
        if result.failures:
            raise RuntimeError(
                f"reconcile of {project}: {len(result.failures)} file(s) failed in pass "
                f"{number}: {_describe(result.failures)}"
            )
        if not result.changed:
            return
    changed = sorted(name for name, count in summary.passes[-1].changes.items() if count)
    raise RuntimeError(
        f"reconcile of {project} did not reach a fixed point in {max_passes} passes; "
        f"rules still changing files: {', '.join(changed)}"
    )


@flow(name="reconcile")
def reconcile(
    project: str, verify: bool = False, rules: list[str] | None = None
) -> ReconcileSummary:
    """Bring a project's index and storage locations fully into line with its rules.

    Phases: a full index of every active storage location; with ``verify``,
    a re-hash of every ``available`` copy; then repeated passes of the enabled
    rules until a pass changes nothing. See docs/design/rules.md#reconcile-m5.

    Args:
        project: project name.
        verify: also re-hash every available copy (finds silent corruption).
        rules: limit the rule phase to these rule names; None runs every rule.

    Returns:
        The changes made per phase, rule and pass; a second run with nothing
        changed in between reports none.

    Raises:
        NotFoundError: if the project is not in config.
        ValueError: if a name in ``rules`` is not a configured rule.
        RuntimeError: if a file failed, or the rules did not reach a fixed
            point within ``reconcile.max_rule_passes``.
    """
    ctx = get_context()
    cfg = ctx.projects.get(project)
    if cfg is None:
        raise models.NotFoundError(f"project {project!r} is not in config")
    cfg.check_rule_names(rules)
    log = get_run_logger()
    summary = ReconcileSummary(project=project, verify=verify)
    limit = f"reconcile:{project}"
    ensure_concurrency_limit(limit)
    with hp_concurrency(limit):
        with ctx.sessionmaker() as session:
            row = models.get_project(session, project)
            if row is None:
                raise models.NotFoundError(f"project {project!r} is not synced")
            active = {
                loc.name: loc.id
                for loc in models.list_storage_locations(session, project)
                if loc.active
            }
            enabled = {
                rule.name: rule_db.effective_enabled(session, row.id, rule.name, rule.enabled)
                for rule in cfg.rules
            }
        names = [loc.name for loc in cfg.storage_locations if loc.name in active]
        for name in names:
            summary.index_changes[name] = _index_changes(project, name)
            log.info(
                "reconcile %s: index %s changes=%d", project, name, summary.index_changes[name]
            )
        if verify:
            for name in names:
                try:
                    summary.verify_changes[name] = _verify(project, name, active[name])
                except Exception as error:
                    raise RuntimeError(
                        f"reconcile verify of {project}/{name} failed: {error}"
                    ) from error
                log.info(
                    "reconcile %s: verify %s marked unknown=%d",
                    project,
                    name,
                    summary.verify_changes[name],
                )
        wanted = [r.name for r in cfg.rules if rules is None or r.name in rules]
        summary.skipped_rules = [name for name in wanted if not enabled[name]]
        summary.rules = [name for name in wanted if enabled[name]]
        if summary.skipped_rules:
            log.info("reconcile %s: disabled rules skipped: %s", project, summary.skipped_rules)
        _rule_phase(project, summary, cfg.reconcile.max_rule_passes, cfg.reconcile.max_failures)
    log.info("reconcile %s: done, total changes=%d", project, summary.changed)
    return summary

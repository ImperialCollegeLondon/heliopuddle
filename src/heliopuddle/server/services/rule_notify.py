"""Best-effort rule notification after a file mutation commits (SVC-25).

See docs/design/rules.md. The notification runs on a short-lived thread and
never raises to the caller.
"""

from __future__ import annotations

import threading
from collections.abc import Collection, Iterable
from contextvars import Context

from prefect.context import SettingsContext, get_settings_context
from sqlalchemy import select
from sqlalchemy.orm import aliased

from heliopuddle.server import models
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import File, RuleState, utcnow
from heliopuddle.server.services.rule_lookup import _project_id
from heliopuddle.server.services.rule_prefect import RuleDisabledError
from heliopuddle.server.services.run_logging import run_logger

logger = run_logger("heliopuddle.server.services.rule_control")


def _notify(
    ctx: AppContext,
    project: str,
    file_ids: tuple[int, ...],
    storage_locations: frozenset[str] | None = None,
) -> None:
    try:
        from heliopuddle.server.models.rules import select_candidates
        from heliopuddle.server.services import rule_control

        with ctx.sessionmaker() as session:
            project_id = _project_id(session, project)
            changed = aliased(File)
            series = select(changed.series_id).where(
                changed.id.in_(file_ids), changed.project_id == project_id
            )
            affected = list(
                session.scalars(
                    select(File.id).where(File.project_id == project_id, File.series_id.in_(series))
                )
            )
            if not affected:
                return
            matching: list[str] = []
            now = utcnow()
            for rule in ctx.projects[project].rules:
                if storage_locations is not None and rule.source not in storage_locations:
                    continue
                state = session.get(RuleState, (project_id, rule.name))
                if not rule.enabled or (state is not None and not state.enabled):
                    continue
                source = models.get_storage_location(session, project, rule.source)
                if (
                    source is not None
                    and source.active
                    and select_candidates(
                        session,
                        project_id,
                        source.id,
                        rule.match,
                        now=now,
                        file_ids=affected,
                        limit=1,
                    )
                ):
                    matching.append(rule.name)
        for name in matching:
            try:
                rule_control.trigger_rule(ctx, project, name)
            except RuleDisabledError:
                pass
            except Exception:
                logger.exception("Could not trigger changed files for rule %s/%s", project, name)
    except Exception:
        logger.exception("Could not notify rules of changed files in project %s", project)


def notify_file_changes(
    ctx: AppContext,
    project: str,
    file_ids: Iterable[int],
    *,
    storage_locations: Collection[str] | None = None,
) -> None:
    """Best-effort fire-and-forget rule notification after a mutation commits.

    Selection uses the same matcher as rule execution and re-evaluates series
    siblings, including every latest flip when a batch changes several versions.
    Only Prefect owns the resulting work;
    this short-lived thread merely sends control requests outside transactions.
    The process waits for it on exit, so a flow run that ends right after
    publishing a copy still triggers the downstream rules.
    Failures, including dispatch failures, are logged and never reach callers.

    Args:
        ctx: the running application context.
        project: the changed files' project.
        file_ids: logical files affected by committed mutations.
        storage_locations: restrict rule sources to these names for copy-only
            changes. None considers every source; an empty collection is a no-op.
            Omit this restriction for logical-file changes or possible latest flips.
    """
    try:
        cfg = ctx.projects.get(project)
        if cfg is None or not cfg.rules:
            return
        ids = tuple(set(file_ids))
        sources = frozenset(storage_locations) if storage_locations is not None else None
        if not ids or sources == frozenset():
            return
        parent = get_settings_context()
        settings_context = SettingsContext(profile=parent.profile, settings=parent.settings)

        def dispatch() -> None:
            with settings_context:
                _notify(ctx, project, ids, sources)

        # Preserve settings, not clients that close when the caller's context ends.
        # Not a daemon: a flow-run process (or any process) that exits right after
        # the mutation still sends the notification instead of dropping it. Its
        # Prefect requests are bounded by ``_prefect_requests``.
        threading.Thread(
            target=Context().run,
            args=(dispatch,),
            name="hp-rule-notify",
            daemon=False,
        ).start()
    except Exception:
        logger.exception("Could not dispatch rule notification for project %s", project)

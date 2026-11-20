"""Runtime rule switches and row locks; configuration remains read-only."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from heliopuddle.server.database.orm import Project, RuleState


def lock_rule_state(
    session: Session, project_id: int, name: str, *, create: bool = False
) -> RuleState | None:
    """Lock a rule's runtime state, optionally creating its default row.

    An absent row is protected by the project row lock. Read-only submissions,
    including dry runs, do not insert runtime state just to acquire a lock.
    SQLite's write transaction already serializes writers.

    Args:
        session: a write transaction owned by the caller.
        project_id: the rule's project.
        name: the configured rule name.
        create: create the row when changing the runtime switch.

    Returns:
        The locked row, or None for the implicit enabled default.
    """
    statement = (
        select(RuleState)
        .where(RuleState.project_id == project_id, RuleState.rule_name == name)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    state = session.scalar(statement)
    if state is None:
        session.execute(select(Project.id).where(Project.id == project_id).with_for_update())
        state = session.scalar(statement)
        if state is None and create:
            state = RuleState(project_id=project_id, rule_name=name, enabled=True)
            session.add(state)
            session.flush()
    return state

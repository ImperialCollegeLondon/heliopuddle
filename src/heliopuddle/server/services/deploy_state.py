"""Database reads that deployments are built from (SVC-09, SVC-13).

Read-only: storage-location ids, effective rule switches and the storage
locations an index adoption run is still due for.
"""

from __future__ import annotations

from typing import NamedTuple

from heliopuddle.server.context import AppContext


def storage_location_ids(ctx: AppContext) -> dict[tuple[str, str], tuple[int, int]]:
    """Return the DB ids of every storage location, by ``(project, storage location)``.

    Read-only; config sync has created the rows.

    Args:
        ctx: the app context.
    """
    from sqlalchemy import select

    from heliopuddle.server.database.orm import Project, StorageLocation

    stmt = select(Project.name, StorageLocation.name, Project.id, StorageLocation.id).join(
        Project, StorageLocation.project_id == Project.id
    )
    with ctx.sessionmaker() as session:
        return {(p, sl): (pid, slid) for p, sl, pid, slid in session.execute(stmt).all()}


def rule_enablement(ctx: AppContext) -> dict[tuple[int, str], bool]:
    """Read effective YAML/DB rule enablement without resetting persistent switches."""
    from sqlalchemy import select

    from heliopuddle.server.database.orm import Project, RuleState

    with ctx.sessionmaker() as session:
        ids = dict(session.execute(select(Project.name, Project.id)).all())
        switches = {
            (state.project_id, state.rule_name): state.enabled
            for state in session.scalars(select(RuleState))
        }
    return {
        (ids[project], rule.name): rule.enabled and switches.get((ids[project], rule.name), True)
        for project, cfg in ctx.projects.items()
        for rule in cfg.rules
    }


class AdoptionTarget(NamedTuple):
    """A storage location to adopt: names and DB ids."""

    project: str
    storage_location: str
    project_id: int
    storage_location_id: int


def storage_locations_to_adopt(ctx: AppContext) -> list[AdoptionTarget]:
    """Return the active storage locations never fully indexed.

    These have no ``index_progress`` row, or one without ``last_full_at``
    (a storage URL change deletes the row). Only projects in the loaded
    config count. Read-only.

    Args:
        ctx: the app context.
    """
    from sqlalchemy import select

    from heliopuddle.server.database.orm import IndexProgress, Project, StorageLocation

    stmt = (
        select(Project.name, StorageLocation.name, Project.id, StorageLocation.id)
        .join(Project, StorageLocation.project_id == Project.id)
        .outerjoin(IndexProgress, IndexProgress.storage_location_id == StorageLocation.id)
        .where(
            StorageLocation.active.is_(True),
            Project.name.in_(list(ctx.projects)),
            IndexProgress.last_full_at.is_(None),
        )
        .order_by(StorageLocation.id)
    )
    with ctx.sessionmaker() as session:
        return [AdoptionTarget(*row) for row in session.execute(stmt).all()]

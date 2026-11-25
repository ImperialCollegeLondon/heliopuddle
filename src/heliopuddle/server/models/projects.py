"""Projects and storage locations in the DB, synced from config (DB-03)."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from heliopuddle.config import ProjectConfig
from heliopuddle.server.database.orm import IndexProgress, Project, RuleState, StorageLocation

logger = logging.getLogger(__name__)


def sync_config(session: Session, projects: Mapping[str, ProjectConfig]) -> None:
    """Upsert projects and storage locations from config, by name.

    Storage locations no longer in a project's config are set ``active=False``
    (rows are never deleted) and are reactivated when they return. ``url`` and
    ``accept_external_changes`` follow the config. Projects missing from config
    are left as they are.

    A changed ``url`` logs a warning and resets the storage location's copies
    for a re-check (:func:`heliopuddle.server.models.index.reset_observations`,
    SVC-12).

    Args:
        session: an open session; the caller commits.
        projects: loaded project configs keyed by name.
    """
    for name, cfg in projects.items():
        project = get_project(session, name)
        if project is None:
            project = Project(name=name)
            session.add(project)
            session.flush()
        existing = {loc.name: loc for loc in list_storage_locations(session, name)}
        configured = set()
        for loc_cfg in cfg.storage_locations:
            configured.add(loc_cfg.name)
            row = existing.get(loc_cfg.name)
            if row is None:
                row = StorageLocation(project_id=project.id, name=loc_cfg.name)
                session.add(row)
            elif row.url.rstrip("/") != loc_cfg.display_url.rstrip("/"):
                _url_changed(session, row, name, loc_cfg.display_url)
            elif row.path_prefix != loc_cfg.path_prefix:
                # Every copy's place in the store moved (STO-23).
                storage_location_moved(
                    session,
                    row,
                    name,
                    repr(row.path_prefix),
                    repr(loc_cfg.path_prefix),
                    what="path_prefix",
                )
                _restart_rules_into(session, project.id, cfg, loc_cfg.name)
            row.url = loc_cfg.display_url
            row.path_prefix = loc_cfg.path_prefix
            row.accept_external_changes = loc_cfg.accept_external_changes
            row.active = True
        for loc_name, row in existing.items():
            if loc_name not in configured:
                row.active = False
    session.flush()


def _restart_rules_into(
    session: Session, project_id: int, cfg: ProjectConfig, destination: str
) -> None:
    """Forget the incremental watermark of every rule copying or moving into ``destination``.

    Its ``path_prefix`` changed (STO-23), so files that were never candidates
    may now be: the next run is a full one rather than one that only looks
    at files changed since the old watermark.
    """
    names = [r.name for r in cfg.rules if r.destination == destination]
    if names:
        session.execute(
            update(RuleState)
            .where(RuleState.project_id == project_id, RuleState.rule_name.in_(names))
            .values(watermark_at=None, watermark_id=None)
        )


def _url_changed(session: Session, row: StorageLocation, project: str, new_url: str) -> None:
    """Treat a storage location's new URL as "the disk moved, with its data" (SVC-12)."""
    storage_location_moved(session, row, project, row.url, new_url, what="URL")


def storage_location_moved(
    session: Session,
    row: StorageLocation,
    project: str,
    old: str,
    new: str,
    *,
    what: str = "root",
) -> None:
    """Reset a storage location whose root moved from ``old`` to ``new`` (SVC-12, STO-19).

    Used for a changed configured ``url`` and for a block-configured storage
    location whose resolved root changed. Copies become pending re-check
    (hash and size kept, observations cleared, generation bumped) and index
    progress is dropped. ``old`` and ``new`` carry no credentials.

    Args:
        session: a session in a write transaction; the caller commits.
        row: the storage location row.
        project: project name, for the log.
        old: the previous root, for the log.
        new: the new root, for the log.
        what: what changed, for the log (``URL``, ``path_prefix`` or ``root``).
    """
    from heliopuddle.server.models.index import reset_observations

    # Match the index checkpoint lock, without blocking new FK references on
    # Postgres (FOR NO KEY UPDATE): a reset cannot miss a concurrent first insert.
    session.refresh(row, with_for_update={"key_share": True})
    count = reset_observations(session, row.id)
    logger.warning(
        "The %s of storage location %r of project %r changed from %s to %s. hp assumes the "
        "data moved with it: %d copies are now pending re-check (hash and size kept, "
        "observations cleared) and index progress is reset, so the next full index (the "
        "adoption run on start) re-checks every copy against the new root: same content "
        "becomes available, different content unknown, absent copies deleted or missing.",
        what,
        row.name,
        project,
        old,
        new,
        count,
    )


def get_project(session: Session, name: str) -> Project | None:
    """Return the project named ``name``, or None."""
    return session.scalar(select(Project).where(Project.name == name))


def list_storage_locations(session: Session, project: str) -> Sequence[StorageLocation]:
    """Return a project's storage locations (active and inactive), in creation order."""
    return session.scalars(
        select(StorageLocation)
        .join(Project)
        .where(Project.name == project)
        .order_by(StorageLocation.id)
    ).all()


def get_storage_location(session: Session, project: str, name: str) -> StorageLocation | None:
    """Return a project's storage location by name, or None."""
    return session.scalar(
        select(StorageLocation)
        .join(Project)
        .where(Project.name == project, StorageLocation.name == name)
    )


def get_index_progress(
    session: Session, storage_location_ids: Sequence[int]
) -> dict[int, IndexProgress]:
    """Return the index watermarks of some storage locations, keyed by storage location id.

    Storage locations never indexed have no entry.
    """
    if not storage_location_ids:
        return {}
    rows = session.scalars(
        select(IndexProgress).where(IndexProgress.storage_location_id.in_(storage_location_ids))
    )
    return {row.storage_location_id: row for row in rows}

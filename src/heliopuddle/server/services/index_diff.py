"""Per-object index diff rules: observe one object, then apply one guarded DB change.

Used by the ``index_batch`` task (``index_tasks.py``); the rules are in
docs/design/indexing.md. Each object is observed once (stat, hash, stat)
outside any transaction, and each file's DB change is one guarded write
transaction that expects the copy's generation.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, BinaryIO, Literal, cast

from prefect import get_run_logger

from heliopuddle.config import ProjectConfig, StorageLocationConfig
from heliopuddle.config.models import VersionRule
from heliopuddle.server import models
from heliopuddle.server.context import AppContext
from heliopuddle.server.models import index as index_db
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services.index_models import IndexCounts, IndexEntry
from heliopuddle.server.services.index_names import SYSTEM_USER
from heliopuddle.storage import StorageLocation
from heliopuddle.versioning import InvalidVersionError, derive


class _CountingReader:
    def __init__(self, stream: BinaryIO, store: HashingStore) -> None:
        self.stream, self.store = stream, store

    def read(self, size: int = -1) -> bytes:
        data = self.stream.read(size)
        self.store.bytes_read += len(data)
        return data

    def __enter__(self) -> _CountingReader:
        return self

    def __exit__(self, *args: Any) -> None:
        self.stream.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.stream, name)


class HashingStore(StorageLocation):
    """Count content actually returned to hash readers, never metadata sizes."""

    bytes_read: int = 0

    def open_read(self, path: str) -> BinaryIO:
        return cast(BinaryIO, _CountingReader(super().open_read(path), self))


@dataclass(frozen=True)
class Location:
    project: ProjectConfig
    config: StorageLocationConfig
    project_id: int
    storage_location_id: int

    @property
    def store(self) -> StorageLocation:
        return StorageLocation(
            self.config.url,
            self.config.storage_options,
            self.config.create_root,
        )


def resolve_location(ctx: AppContext, project: str, storage_location: str) -> Location:
    project_cfg = ctx.projects.get(project)
    location_cfg = ctx.storage_location_config(project, storage_location)
    if project_cfg is None or location_cfg is None:
        raise LookupError(f"storage location {project}/{storage_location} is not in config")
    with ctx.sessionmaker() as session:
        row = models.get_storage_location(session, project, storage_location)
        if row is None or not row.active:
            raise models.NotFoundError(f"storage location {project}/{storage_location} not found")
        if row.url.rstrip("/") != location_cfg.url.rstrip("/"):
            raise RuntimeError(f"storage location {project}/{storage_location} config changed")
        return Location(project_cfg, location_cfg, row.project_id, row.id)


# --- observing one object -------------------------------------------------------


@dataclass(frozen=True)
class Observation:
    """One consistent observation of an object: size, mtime and md5 together (SVC-16)."""

    size: int
    mtime: datetime | None
    md5: str


def observe(store: StorageLocation, entry: IndexEntry, *, read: bool = False) -> Observation | None:
    """Observe ``entry``'s object once: stat, hash, stat (SVC-16).

    A backend md5 reported by the listing itself is one consistent
    observation with the listed size and mtime, so it is used as is (D6)
    unless ``read`` asks for the content to be read. Otherwise the object is
    stat-ed (it must still match the listing), read and hashed, and stat-ed
    again.

    Returns:
        The observation, or None if the object is gone or changed meanwhile
        (the entry is left for the next run).
    """
    if entry.md5 and not read:
        return Observation(entry.size, entry.mtime, entry.md5.lower())
    try:
        before = store.info(entry.path)
        if (before.size, before.mtime) != (entry.size, entry.mtime):
            return None
        digest = store.md5(entry.path)
        after = store.info(entry.path)
    except FileNotFoundError:
        return None
    if after != before:
        return None
    return Observation(before.size, before.mtime, digest.lower())


# --- per-file diff rules --------------------------------------------------------

Outcome = Literal["added", "added_unknown", "changed", "unknown", "updated", "skipped", "rechecked"]


def _version_problem(path: str, rules: Sequence[VersionRule]) -> str | None:
    """Return why the version rules cannot derive a version for ``path`` (DEP-12), or None.

    A version of the other kind than its series (API-21) is found only under
    the series lock, when the file is added.
    """
    try:
        derive(path, rules)
    except InvalidVersionError as e:
        return str(e) or type(e).__name__
    return None


def index_new(
    ctx: AppContext, loc: Location, store: StorageLocation, entry: IndexEntry, seen_at: datetime
) -> Outcome:
    logger = get_run_logger()
    try:
        models.validate_path(entry.path)
    except ValueError:
        logger.warning("index: skipping invalid path %r", entry.path)
        return "skipped"
    seen = observe(store, entry)
    if seen is None:
        return "skipped"
    accept = loc.config.accept_external_changes
    rules: Sequence[VersionRule] = loc.project.versioning
    problem = _version_problem(entry.path, rules)
    if problem is None:
        try:
            added = _add_new(ctx, loc, entry, seen, seen_at, rules, accept)
        except models.VersionKindMismatch as e:
            problem = str(e)
        else:
            if not added:
                return "skipped"
            return "added" if accept else "added_unknown"
    # Recorded unversioned in its literal-path series, as ``unknown`` (never
    # latest); an operator can fix the rule or the file and accept it.
    logger.warning(
        "index: %s: the version rules give no valid version (%s); recorded as unknown",
        entry.path,
        problem,
    )
    if not _add_new(ctx, loc, entry, seen, seen_at, (), False):
        return "skipped"
    return "added_unknown"


def _add_new(
    ctx: AppContext,
    loc: Location,
    entry: IndexEntry,
    seen: Observation,
    seen_at: datetime,
    rules: Sequence[VersionRule],
    accept: bool,
) -> bool:
    """Record a new file in one transaction; False if one was registered meanwhile."""
    with ctx.write_session() as session:
        if (
            index_db.get_index_target(session, loc.project_id, loc.storage_location_id, entry.path)
            is not None
        ):
            return False  # registered meanwhile; looked at again next run
        index_db.index_add_file(
            session,
            project=loc.project.project,
            storage_location=loc.config.name,
            path=entry.path,
            hash=seen.md5,
            size=seen.size,
            mtime=seen.mtime,
            state=FileState.AVAILABLE if accept else FileState.UNKNOWN,
            rules=rules,
            user=SYSTEM_USER,
            seen_at=seen_at,
        )
    return True


def _apply(
    ctx: AppContext,
    loc: Location,
    entry: IndexEntry,
    target: index_db.IndexTarget,
    *,
    state: FileState,
    seen: Observation | None,
    seen_at: datetime,
    new_file_hash: bool = False,
) -> bool:
    """Apply one copy change, guarded by what was read (generation, state, file).

    Returns:
        False if the DB changed since ``target`` was read (nothing applied).
    """
    with ctx.write_session() as session:
        if not index_db.lock_index_target(
            session, loc.project_id, loc.storage_location_id, entry.path, target
        ):
            get_run_logger().debug("index: %s changed meanwhile; skipped", entry.path)
            return False
        index_db.index_set_copy(
            session,
            file_id=target.file_id,
            storage_location_id=loc.storage_location_id,
            state=state,
            size=seen.size if seen is not None else entry.size,
            mtime=seen.mtime if seen is not None else entry.mtime,
            hash=seen.md5 if seen is not None and state == FileState.AVAILABLE else None,
            seen_at=seen_at,
            new_file_hash=new_file_hash,
            user=SYSTEM_USER,
        )
    return True


def index_known(
    ctx: AppContext,
    loc: Location,
    store: StorageLocation,
    entry: IndexEntry,
    target: index_db.IndexTarget,
    seen_at: datetime,
) -> Outcome:
    accept = loc.config.accept_external_changes
    no_copy = target.copy_id is None or target.copy_state == FileState.DELETED
    seen: Observation | None = None
    new_file_hash = False
    if no_copy:
        outcome: Outcome = "added"
        if accept:
            seen = observe(store, entry)
            if seen is None:
                return "skipped"
            state = FileState.AVAILABLE if seen.md5 == target.file_hash else FileState.UNKNOWN
        else:
            state = FileState.UNKNOWN
        if state == FileState.UNKNOWN:
            outcome = "added_unknown"
    else:
        # The copy was missing, or its size or mtime changed.
        if accept:
            seen = observe(store, entry)
            if seen is None:
                return "skipped"
            new_file_hash = seen.md5 != target.file_hash
            state = FileState.AVAILABLE
            outcome = "changed" if new_file_hash else "updated"
        else:
            state = FileState.UNKNOWN
            outcome = "updated" if target.copy_state == FileState.UNKNOWN else "unknown"
    if not _apply(
        ctx,
        loc,
        entry,
        target,
        state=state,
        seen=seen,
        seen_at=seen_at,
        new_file_hash=new_file_hash,
    ):
        return "skipped"
    return outcome


def index_recheck(
    ctx: AppContext,
    loc: Location,
    store: StorageLocation,
    entry: IndexEntry,
    target: index_db.IndexTarget,
    seen_at: datetime,
) -> Outcome:
    """Re-check a ``pending`` copy never observed in this root (SVC-12).

    The object is read (never a backend md5) and compared with the file's
    kept hash and size: both match → ``available``, else ``unknown``. The
    same in accepting and non-accepting storage locations: the content is
    verified, not accepted.
    """
    seen = observe(store, entry, read=True)
    if seen is None:
        return "skipped"
    matches = seen.md5 == target.file_hash.lower() and seen.size == target.file_size
    state = FileState.AVAILABLE if matches else FileState.UNKNOWN
    if not _apply(ctx, loc, entry, target, state=state, seen=seen, seen_at=seen_at):
        return "skipped"
    return "rechecked" if matches else "unknown"


def count_outcome(counts: IndexCounts, outcome: Outcome) -> None:
    if outcome in ("added", "added_unknown"):
        counts.added += 1
    if outcome in ("added_unknown", "unknown"):
        counts.unknown += 1
    if outcome == "changed":
        counts.changed += 1
    if outcome == "skipped":
        counts.skipped += 1
    if outcome == "rechecked":
        counts.reverified += 1


NOT_DIFFED = (FileState.PENDING, FileState.OVERWRITING, FileState.DELETING)
"""Copy states the diff never touches (except a ``pending`` copy awaiting re-check)."""

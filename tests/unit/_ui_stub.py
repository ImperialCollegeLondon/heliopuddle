"""The stub client and sample data behind the UI unit tests."""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from heliopuddle.client import NotFoundError
from heliopuddle.server.schemas import (
    FileList,
    FilePreview,
    FileRead,
    FileState,
    FileStorageLocationRead,
    PreviewColumn,
    PreviewTable,
    ProjectRead,
    ProjectSummary,
    RuleRead,
    RunInfo,
    SearchResults,
    SeriesVersions,
    StorageLocationRead,
    StorageLocationSummary,
)
from heliopuddle.server.schemas.search import ViewCounts

UI_DIR = Path(__file__).resolve().parents[2] / "src" / "heliopuddle" / "server" / "ui"


NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


LATER = datetime(2026, 2, 3, 4, 5, 6, tzinfo=UTC)


RUN_ID = "0b7c3f5e-1d2a-4c3b-9e8f-7a6b5c4d3e2f"


SAME_ORIGIN = {"Origin": "http://testserver"}


def make_file(
    i: int, *, state: FileState | None = FileState.AVAILABLE, project: str = "demo", **extra: Any
) -> FileRead:
    copies = (
        [FileStorageLocationRead(storage_location="primary", state=state, updated_at=NOW)]
        if state is not None
        else []
    )
    values: dict[str, Any] = {
        "id": i,
        "project": project,
        "path": f"data/file-{i}.csv",
        "name": f"file-{i}.csv",
        "series_id": i,
        "size": 1536 * i,
        "hash": "abc",
        "hash_algo": "md5",
        "is_latest": True,
        "created_at": NOW,
        "updated_at": NOW,
        "created_by": "alice" if i % 2 else None,
        "storage_locations": copies,
    }
    return FileRead(**{**values, **extra})


def copy(sl: str, state: FileState, **extra: Any) -> FileStorageLocationRead:
    return FileStorageLocationRead(storage_location=sl, state=state, updated_at=NOW, **extra)


def file_with(i: int, *states: tuple[str, FileState], **extra: Any) -> FileRead:
    """A file with one copy per ``(storage location, state)``."""
    return make_file(i, storage_locations=[copy(sl, st) for sl, st in states], **extra)


def sl_read(name: str, **extra: Any) -> StorageLocationRead:
    values: dict[str, Any] = {
        "name": name,
        "url": f"file:///srv/{name}",
        "accept_external_changes": True,
        "active": True,
    }
    return StorageLocationRead(**{**values, **extra})


def _states(f: FileRead) -> dict[str, FileState]:
    return {c.storage_location: c.state for c in f.storage_locations}


@dataclass
class StubClient:
    """Stands in for HelioPuddleClient; records how it was built and used."""

    project: str | None
    user: str | None
    projects: list[str] = field(default_factory=lambda: ["demo"])
    files: list[FileRead] = field(default_factory=list)
    error: Exception | None = None
    storage_locations: list[StorageLocationRead] = field(
        default_factory=lambda: [
            StorageLocationRead(
                name="primary", url="file:///data", accept_external_changes=True, active=True
            )
        ]
    )
    action_error: Exception | None = None
    calls: list[tuple[str, Any]] = field(default_factory=list)
    rules: list[RuleRead] = field(default_factory=list)
    closed: bool = False
    token: str | None = None
    summary_error: Exception | None = None
    content: bytes = b"a,b\n1,2\n"
    previews: dict[int, FilePreview] = field(default_factory=dict)
    preview_error: Exception | None = None

    def _maybe_raise(self) -> None:
        if self.error is not None:
            raise self.error

    def list_projects(self) -> list[ProjectRead]:
        self._maybe_raise()
        self.calls.append(("list_projects", None))
        return [
            ProjectRead(name=p, storage_locations=[s.name for s in self.storage_locations])
            for p in self.projects
        ]

    def project_summary(self, project: str | None = None) -> ProjectSummary:
        self._maybe_raise()
        name = project or self.project or "demo"
        self.calls.append(("project_summary", project))
        if self.summary_error is not None:
            raise self.summary_error
        live = [f for f in self.files if f.deleted_at is None and f.project == name]
        sls = []
        for sl in self.storage_locations:
            states = dict.fromkeys(FileState, 0)
            here = [f for f in live if sl.name in _states(f)]
            for f in here:
                states[_states(f)[sl.name]] += 1
            kept = [f for f in here if _states(f)[sl.name] != FileState.DELETED]
            sls.append(
                StorageLocationSummary(
                    name=sl.name,
                    active=sl.active,
                    files=len(kept),
                    bytes=sum(f.size for f in kept),
                    states=states,
                    no_copy=len(live) - len(kept),
                )
            )

        def any_in(f: FileRead, wanted: tuple[FileState, ...]) -> bool:
            return any(c.state in wanted for c in f.storage_locations)

        return ProjectSummary(
            project=name,
            files=len(live),
            latest_files=sum(f.is_latest for f in live),
            bytes=sum(f.size for f in live),
            deleted_files=sum(
                1 for f in self.files if f.deleted_at is not None and f.project == name
            ),
            storage_locations=sls,
            views=ViewCounts(
                needs_attention=sum(
                    any_in(f, (FileState.UNKNOWN, FileState.MISSING)) for f in live
                ),
                in_progress=sum(
                    any_in(f, (FileState.PENDING, FileState.OVERWRITING, FileState.DELETING))
                    for f in live
                ),
                ignored=sum(f.ignored_from_all_rules for f in live),
            ),
        )

    def list_files(self, limit: int = 100, offset: int = 0) -> FileList:
        self._maybe_raise()
        self.calls.append(("list_files", (limit, offset)))
        return FileList(
            items=self.files[offset : offset + limit],
            total=len(self.files),
            limit=limit,
            offset=offset,
        )

    def find(self, **kwargs: Any) -> FileList:
        self._maybe_raise()
        self.calls.append(("find", kwargs))
        limit, offset = kwargs.get("limit", 100), kwargs.get("offset", 0)
        state = kwargs.get("state")
        wanted = (
            None
            if state is None
            else {FileState(s) for s in ([state] if isinstance(state, str) else state)}
        )
        where = kwargs.get("storage_location")
        start, end = kwargs.get("content_date_from"), kwargs.get("content_date_to")
        ignored = kwargs.get("ignored_from_all_rules")

        def copy_ok(f: FileRead) -> bool:
            copies = [
                c for c in f.storage_locations if where is None or c.storage_location == where
            ]
            if wanted is not None:
                return any(c.state in wanted for c in copies)
            if where is not None:
                return any(c.state != FileState.DELETED for c in copies)
            return True

        items = [
            f
            for f in self.files
            if f.project == (self.project or f.project)
            and (kwargs.get("path") is None or fnmatch(f.path, kwargs["path"]))
            and (not kwargs.get("latest_only") or f.is_latest)
            and copy_ok(f)
            and (
                f.deleted_at is not None
                if kwargs.get("deleted_only")
                else kwargs.get("include_deleted") or f.deleted_at is None
            )
            and (start is None or (f.content_date is not None and f.content_date >= start))
            and (end is None or (f.content_date is not None and f.content_date <= end))
            and (ignored is None or f.ignored_from_all_rules == ignored)
        ]
        return FileList(
            items=items[offset : offset + limit], total=len(items), limit=limit, offset=offset
        )

    def search(
        self,
        q: str,
        projects: list[str] | None = None,
        *,
        latest_only: bool = False,
        include_deleted: bool = False,
        limit: int = 20,
        offset: int = 0,
    ) -> SearchResults:
        self._maybe_raise()
        self.calls.append(("search", (q, projects, limit, offset)))
        if projects:
            for p in projects:
                if p not in self.projects:
                    raise NotFoundError(404, f"project {p!r} not found")
        scope = projects or self.projects
        words = [w.lower() for w in q.split()]
        hits = [
            f
            for f in self.files
            if f.project in scope
            and all(w in f.path.lower() for w in words)
            and (include_deleted or f.deleted_at is None)
        ]
        counts: dict[str, int] = {}
        for f in hits:
            counts[f.project] = counts.get(f.project, 0) + 1
        return SearchResults(
            items=hits[offset : offset + limit],
            total=len(hits),
            limit=limit,
            offset=offset,
            projects=counts,
        )

    def get_file(self, file_id: int) -> FileRead:
        self._maybe_raise()
        self.calls.append(("get_file", file_id))
        for f in self.files:
            if f.id == file_id:
                return f
        raise NotFoundError(404, f"file {file_id} not found")

    def open(self, path_or_id: str | int) -> Any:
        self.calls.append(("open", path_or_id))
        f = self.get_file(int(path_or_id))
        if not any(c.state == FileState.AVAILABLE for c in f.storage_locations):
            raise FileNotFoundError("no available copy")
        return io.BytesIO(self.content)

    def preview(self, path_or_id: str | int) -> FilePreview:
        """The preview set for the file, else a one-row table."""
        file_id = int(path_or_id)
        self.calls.append(("preview", file_id))
        if self.preview_error is not None:
            raise self.preview_error
        if file_id in self.previews:
            return self.previews[file_id]
        return FilePreview(
            file_id=file_id,
            kind="table",
            format="csv",
            tables=[PreviewTable(columns=[PreviewColumn(name="a")], rows=[["1"]], more_rows=False)],
        )

    def list_versions(
        self, path_or_series_id: str | int, include_deleted: bool = False
    ) -> SeriesVersions:
        self._maybe_raise()
        self.calls.append(("list_versions", (path_or_series_id, include_deleted)))
        items = sorted(
            (
                f
                for f in self.files
                if f.series_id == path_or_series_id and (include_deleted or f.deleted_at is None)
            ),
            key=lambda f: f.id,
            reverse=True,
        )
        return SeriesVersions(
            series_id=int(path_or_series_id),
            series_key="data/x_v{v}.csv",
            project="demo",
            items=items,
        )

    def list_storage_locations(self) -> list[StorageLocationRead]:
        self._maybe_raise()
        self.calls.append(("list_storage_locations", None))
        return self.storage_locations

    def trigger_index(self, storage_location: str | None = None, mode: str = "full") -> RunInfo:
        self.calls.append(("trigger_index", (storage_location, mode)))
        if self.action_error is not None:
            raise self.action_error
        return RunInfo(
            flow_run_id=RUN_ID,
            flow_run_url=f"http://prefect/runs/flow-run/{RUN_ID}",
            deployment=f"index_{mode}/index-{mode}-demo-{storage_location}",
        )

    def accept_file(self, path_or_id: str | int, storage_location: str | None = None) -> FileRead:
        self.calls.append(("accept_file", (path_or_id, storage_location)))
        if self.action_error is not None:
            raise self.action_error
        return self.get_file(int(path_or_id))

    def list_rules(self) -> list[RuleRead]:
        self._maybe_raise()
        self.calls.append(("list_rules", None))
        return self.rules

    def enable_rule(self, name: str) -> RuleRead:
        self.calls.append(("enable_rule", name))
        if self.action_error:
            raise self.action_error
        return self.rules[0]

    def disable_rule(self, name: str) -> RuleRead:
        self.calls.append(("disable_rule", name))
        if self.action_error:
            raise self.action_error
        return self.rules[0]

    def run_rule(self, name: str, mode: str, dry_run: bool) -> RunInfo:
        self.calls.append(("run_rule", (name, mode, dry_run)))
        if self.action_error:
            raise self.action_error
        return RunInfo(flow_run_id=RUN_ID, deployment="run_rule/rule-p1-demo-archive")

    def reconcile(self, verify: bool = False, rules: list[str] | None = None) -> RunInfo:
        self.calls.append(("reconcile", (verify, rules)))
        if self.action_error:
            raise self.action_error
        return RunInfo(flow_run_id=RUN_ID, deployment="reconcile/reconcile-p1-demo")

    def run_once(self, spec: Any) -> RunInfo:
        self.calls.append(("run_once", spec))
        if self.action_error:
            raise self.action_error
        return RunInfo(flow_run_id=RUN_ID, deployment="run_rule_once/rule-once")

    def set_rules_ignored(self, path_or_id: int, ignored: bool) -> FileRead:
        self.calls.append(("set_rules_ignored", (path_or_id, ignored)))
        if self.action_error:
            raise self.action_error
        return self.get_file(path_or_id)

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> StubClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

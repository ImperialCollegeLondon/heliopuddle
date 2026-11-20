"""Names shared by the index deployments, the runs API and the runtime (M3)."""

from __future__ import annotations

from typing import Literal

IndexMode = Literal["full", "incremental"]

INDEX_FLOW_NAMES: dict[IndexMode, str] = {
    "full": "index_full",
    "incremental": "index_incremental",
}

SYSTEM_USER = "heliopuddle:index"
"""``created_by``/``updated_by`` for rows written by the indexer."""


def index_deployment_name(
    mode: IndexMode,
    project_id: int,
    storage_location_id: int,
    project: str,
    storage_location: str,
) -> str:
    """Return the deployment name, e.g. ``index-full-p1-sl2-demo-primary`` (SVC-13).

    The ids make the name unique (``alpha-beta``/``primary`` and
    ``alpha``/``beta-primary`` differ); the names keep it readable.
    """
    return f"index-{mode}-p{project_id}-sl{storage_location_id}-{project}-{storage_location}"


def index_deployment_full_name(
    mode: IndexMode,
    project_id: int,
    storage_location_id: int,
    project: str,
    storage_location: str,
) -> str:
    """Return ``<flow>/<deployment>``, as used by ``run_deployment``."""
    name = index_deployment_name(mode, project_id, storage_location_id, project, storage_location)
    return f"{INDEX_FLOW_NAMES[mode]}/{name}"


def is_index_deployment_name(mode: IndexMode, name: str) -> bool:
    """True for an index deployment name of ``mode``, current or older format.

    Older releases named them ``index-<mode>-<project>-<storage location>``;
    both formats start with ``index-<mode>-``.
    """
    return name.startswith(f"index-{mode}-")


def storage_location_lock(project: str, storage_location: str) -> str:
    """Return the concurrency limit name for a storage location."""
    return f"storage-location:{project}:{storage_location}"

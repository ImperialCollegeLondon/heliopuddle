"""Shared helpers and fixtures for the split index_deploy tests."""

from __future__ import annotations

import sys
import types
from datetime import datetime
from typing import Any

import pytest
from prefect import flow

from heliopuddle.config import ProjectConfig
from heliopuddle.server.services import deploy
from heliopuddle.server.services.deploy import (
    INDEX_MODULE,
)


@flow(name="index_full")
def index_full(project: str, storage_location: str) -> dict[str, int]:
    """Stand-in for the real flow."""
    return {}


@flow(name="index_incremental")
def index_incremental(
    project: str, storage_location: str, since: datetime | None = None
) -> dict[str, int]:
    """Stand-in for the real flow."""
    return {}


@pytest.fixture(autouse=True)
def _no_reconcile_deployments(monkeypatch: pytest.MonkeyPatch) -> None:
    """Index deployment tests leave the reconcile flow out (see test_reconcile_deploy.py)."""
    monkeypatch.setattr(deploy, "RECONCILE_MODULE", "heliopuddle.server.services.no_reconcile")


@pytest.fixture
def index_module(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Install stand-in index flows as ``heliopuddle.server.services.index``."""
    module = types.ModuleType(INDEX_MODULE)
    module.index_full = index_full  # type: ignore[attr-defined]
    module.index_incremental = index_incremental  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, INDEX_MODULE, module)
    return module


def _project(name: str, locations: list[str], **index: Any) -> ProjectConfig:
    return ProjectConfig.model_validate(
        {
            "project": name,
            "storage_locations": [{"name": n, "url": f"memory://{name}/{n}"} for n in locations],
            "index": index,
        }
    )

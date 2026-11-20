"""Shared helpers and fixtures for the split rule_engine tests."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from heliopuddle.config.models import (
    RuleConfig,
)
from heliopuddle.server import models
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import File, FileStorageLocation
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services import rule_checkpoint

NOW = datetime(2026, 10, 30, 12, tzinfo=UTC)


OLD = NOW - timedelta(days=40)


def seed(ctx: AppContext, path: str, **values: Any) -> int:
    """Make an available source copy with deterministic observation and change times."""
    copy_values = values.pop("copy", {})
    with ctx.write_session() as session:
        result = models.register_file(
            session,
            project="demo",
            storage_location="primary",
            path=path,
            hash="ab" * 16,
            size=10,
        )
        file = session.get_one(File, result.file_id)
        file.created_at = OLD
        file.updated_at = OLD
        file.content_date = OLD
        for key, value in values.items():
            setattr(file, key, value)
        copy = session.get_one(FileStorageLocation, result.copy_id)
        copy.state = FileState.AVAILABLE
        copy.size = file.size
        copy.hash = file.hash
        copy.mtime = OLD
        copy.arrived_at = OLD
        copy.updated_at = OLD
        for key, value in copy_values.items():
            setattr(copy, key, value)
        return file.id


def configured(ctx: AppContext, **options: Any) -> RuleConfig:
    rule = RuleConfig.model_validate(
        {
            "name": "archive",
            "action": "copy",
            "source": "primary",
            "destination": "secondary",
            "match": {"paths": ["**"]},
            "options": options,
        }
    )
    ctx.projects["demo"].rules = [rule]
    return rule


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    from heliopuddle.server.services import rules

    calls: list[int] = []
    successors: list[dict[str, Any]] = []
    locks: list[str] = []

    @contextmanager
    def lock(name: str, **kwargs: Any):
        locks.append(name)
        yield

    def perform(candidate, spec, **kwargs):
        calls.append(candidate.file_id)
        return SimpleNamespace(
            file_id=candidate.file_id, path=candidate.path, outcome="copied", detail=None
        )

    monkeypatch.setattr(rules, "get_run_logger", lambda: logging.getLogger("test.rules"))
    monkeypatch.setattr(rules, "hp_concurrency", lock)
    monkeypatch.setattr(rules, "ensure_concurrency_limit", lambda *a, **k: None)
    monkeypatch.setattr(rules, "utcnow", lambda: NOW)
    monkeypatch.setattr(rule_checkpoint, "utcnow", lambda: NOW)
    monkeypatch.setattr(rules, "_execute_file", perform)
    monkeypatch.setattr(
        rules, "_schedule_successor", lambda *a, **kw: successors.append(kw["parameters"])
    )
    return SimpleNamespace(module=rules, calls=calls, successors=successors, locks=locks)

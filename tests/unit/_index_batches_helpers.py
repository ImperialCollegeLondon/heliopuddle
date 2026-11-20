"""Shared helpers and fixtures for the split index_batches tests."""

from __future__ import annotations

import logging
import os
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from heliopuddle.config.models import BatchConfig
from heliopuddle.server.context import AppContext
from heliopuddle.server.services import (
    index,
    index_diff,
    index_finish,
    index_run,
    index_tasks,
)


def put(root: Path, path: str, data: bytes = b"0123456789") -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    old = (datetime.now(UTC) - timedelta(hours=2)).timestamp()
    os.utime(target, (old, old))


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    batches: list[list[str]] = []
    successors: list[dict[str, Any]] = []
    real_batch, real_removals = index_tasks.index_batch, index_tasks.index_removals

    def task_result(fn, *args, **kwargs):
        state = kwargs.pop("return_state", False)
        try:
            result = fn(*args, **kwargs)
        except Exception as error:
            if not state:
                raise
            return SimpleNamespace(
                is_completed=lambda: False,
                message=str(error),
                result=lambda error=error, **kw: error,
            )
        return (
            SimpleNamespace(is_completed=lambda: True, result=lambda **kw: result)
            if state
            else result
        )

    def batch(*args, **kwargs):
        batches.append([entry.path for entry in args[2]])
        return task_result(real_batch.fn, *args, **kwargs)

    monkeypatch.setattr(index_run, "index_batch", batch)
    monkeypatch.setattr(
        index_finish, "index_removals", lambda *a, **k: task_result(real_removals.fn, *a, **k)
    )
    for module in (index, index_diff, index_finish, index_run, index_tasks):
        monkeypatch.setattr(module, "get_run_logger", lambda: logging.getLogger("test.index"))
    monkeypatch.setattr(index, "_publish", lambda summary: None)
    monkeypatch.setattr(index, "ensure_concurrency_limit", lambda *a, **k: None)
    monkeypatch.setattr(index, "hp_concurrency", lambda *a, **k: nullcontext())
    monkeypatch.setattr(
        index,
        "_schedule_successor",
        lambda *a, **kw: successors.append(kw["parameters"]),
        raising=False,
    )
    return SimpleNamespace(batches=batches, successors=successors, batch=real_batch.fn)


def limits(ctx: AppContext, *, batch_size: int = 17, retrigger: bool = True, **kw: Any) -> None:
    cfg = ctx.projects["demo"].index
    cfg.batch = BatchConfig.model_validate(kw)
    cfg.batch_size = batch_size
    cfg.retrigger = retrigger

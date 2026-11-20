"""Shared helpers and fixtures for the split adv_reconcile tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from heliopuddle.config.models import RuleConfig
from tests.unit.test_reconcile import (  # noqa: F401 - fixtures
    config_dir,
    dump,
    no_triggers,
    present,
    roots,
    snapshot_stores,
    state,
    use,
)


def rl(name: str, action: str, source: str, destination: str | None = None, **opts: Any) -> Any:
    """A rule over ``move/**`` with test-friendly options (``opts`` override them)."""
    options = {"pause_between_runs": "0s", "check_for_space": False, **opts}
    return RuleConfig.model_validate(
        {
            "name": name,
            "action": action,
            "source": source,
            "destination": destination,
            "match": {"paths": ["move/**"]},
            "options": options,
        }
    )


def content(root: Path, path: str) -> bytes | None:
    target = root / path
    return target.read_bytes() if target.exists() else None

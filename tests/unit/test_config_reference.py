"""The documented reference project sets every config option and validates (DEP-05, DEP-06)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from heliopuddle.config import load_project
from heliopuddle.config.models import ProjectConfig

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "config-reference" / "project.yaml"


def _collect(value: Any, seen: dict[type, set[str]]) -> None:
    """Record, per model class, every field that was set explicitly."""
    if isinstance(value, BaseModel):
        seen.setdefault(type(value), set()).update(value.model_fields_set)
        for name in type(value).model_fields:
            _collect(getattr(value, name), seen)
    elif isinstance(value, list):
        for item in value:
            _collect(item, seen)


@pytest.mark.req("DEP-05")
def test_reference_example_loads() -> None:
    project = load_project(EXAMPLE)

    assert project.project == "reference"
    assert {r.action for r in project.rules} == {"copy", "move", "delete"}


@pytest.mark.req("DEP-05", "DEP-06")
def test_reference_example_sets_every_option() -> None:
    seen: dict[type, set[str]] = {}
    _collect(load_project(EXAMPLE), seen)

    missing = {
        f"{model.__name__}.{field}"
        for model, fields in seen.items()
        for field in set(model.model_fields) - fields
    }
    # Every nested model type must be reached, not just the top level.
    assert len(seen) >= 10
    assert not missing, f"document these options in {EXAMPLE}: {sorted(missing)}"
    assert set(ProjectConfig.model_fields) <= seen[ProjectConfig]

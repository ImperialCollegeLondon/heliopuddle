"""The environment variable tables in the user docs match the settings models."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("env_table", ROOT / "scripts" / "env_table.py")
assert _spec is not None and _spec.loader is not None
env_table = importlib.util.module_from_spec(_spec)
sys.modules["env_table"] = env_table
_spec.loader.exec_module(env_table)


def test_docs_page_is_up_to_date() -> None:
    page = (ROOT / "docs" / "site" / "environment.md").read_text(encoding="utf-8")
    assert env_table.embedded(page) == env_table.render(), (
        "docs/site/environment.md is stale: run `uv run python scripts/env_table.py --write`"
    )


def test_every_setting_is_listed() -> None:
    names = {name for name, _, _ in env_table.server_rows()}
    assert "HELIOPUDDLE_DATABASE_URL" in names
    assert all(name.startswith("HELIOPUDDLE_") for name in names)

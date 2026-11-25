"""scripts/req_matrix.py orders lettered milestones (M5A sits between M5 and M6)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("req_matrix", ROOT / "scripts" / "req_matrix.py")
assert _spec is not None and _spec.loader is not None
req_matrix = importlib.util.module_from_spec(_spec)
sys.modules["req_matrix"] = req_matrix
_spec.loader.exec_module(req_matrix)


def test_lettered_milestone_sorts_between_its_number_and_the_next() -> None:
    key = req_matrix.milestone_key
    assert key("M5") < key("M5A") < key("M6")
    assert key("M4") < key("M5")
    assert key("M10") > key("M9")


def test_invalid_milestone_is_rejected() -> None:
    with pytest.raises(ValueError, match="invalid milestone"):
        req_matrix.milestone_key("5A")


def test_auth_requirements_are_parsed_with_their_milestone() -> None:
    reqs = req_matrix.load_requirements()
    assert reqs["AUTH-01"][0] == (5, "A")
    assert reqs["CLI-01"][0] == (1, "")
    assert len([r for r in reqs if r.startswith("AUTH-")]) == 21


def test_amended_requirements_keep_their_original_milestone() -> None:
    text = "**UI-01 (M1, amended M6)** The home page.\n**UI-09 (M6)** The shell.\n"
    found = {m["id"]: m["ms"] for m in req_matrix.REQ_RE.finditer(text)}
    assert found == {"UI-01": "M1", "UI-09": "M6"}

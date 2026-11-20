"""DEP-09: the lockfile is current; holds are documented; bounds may be loose."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("check_deps", ROOT / "scripts" / "check_deps.py")
assert _spec is not None and _spec.loader is not None
check_deps = importlib.util.module_from_spec(_spec)
sys.modules["check_deps"] = check_deps
_spec.loader.exec_module(check_deps)


@pytest.mark.req("DEP-09")
def test_every_direct_dependency_is_locked(capsys: pytest.CaptureFixture[str]) -> None:
    assert check_deps.main(["--offline"]) == 0, capsys.readouterr().out


@pytest.mark.req("DEP-09")
def test_every_hold_has_a_reason() -> None:
    assert all(reason.strip() for reason in check_deps.HOLDS.values())


@pytest.mark.req("DEP-09")
def test_lagging_dependency_without_hold_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(check_deps, "direct_requirements", lambda: {"pkg": "1.0"})
    monkeypatch.setattr(check_deps, "locked_versions", lambda: {"pkg": "1.0"})
    monkeypatch.setattr(check_deps, "latest_version", lambda name: "2.0")
    assert check_deps.main([]) == 1
    monkeypatch.setattr(check_deps, "HOLDS", {"pkg": "reason"})
    assert check_deps.main([]) == 0


@pytest.mark.req("DEP-09")
def test_a_loose_lower_bound_is_fine_but_an_unlocked_dependency_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(check_deps, "direct_requirements", lambda: {"pkg": "0.5", "free": None})
    monkeypatch.setattr(check_deps, "locked_versions", lambda: {"pkg": "1.0", "free": "3.0"})
    assert check_deps.main(["--offline"]) == 0
    monkeypatch.setattr(check_deps, "locked_versions", lambda: {"pkg": "1.0"})
    assert check_deps.main(["--offline"]) == 1


@pytest.mark.req("DEP-09", "DEP-18")
def test_the_client_floors_are_reported_as_declared() -> None:
    bounds = check_deps.direct_requirements()
    assert bounds["pydantic"] == "2.5.0"
    assert bounds["typer"] == "0.13.0"

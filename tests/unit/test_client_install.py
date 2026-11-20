"""DEP-17: the core install is the client only; the server is the ``server`` extra."""

from __future__ import annotations

import re
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

CLIENT_DEPENDENCIES = {"fsspec", "httpx", "pydantic", "pydantic-settings", "pyyaml"}
SERVER_ONLY = (
    "alembic",
    "cdflib",
    "crump",
    "fastapi",
    "jinja2",
    "jwt",
    "prefect",
    "psycopg",
    "pyarrow",
    "sqlalchemy",
    "typer",
    "uvicorn",
)


def _name(requirement: str) -> str:
    match = re.match(r"[A-Za-z0-9_.-]+", requirement)
    assert match, requirement
    return match.group(0).lower()


@pytest.mark.req("DEP-17")
def test_core_dependencies_are_the_clients_and_the_server_is_an_extra() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    extras = project["optional-dependencies"]

    assert {_name(d) for d in project["dependencies"]} == CLIENT_DEPENDENCIES
    server = {_name(d) for d in extras["server"]}
    for package in ("fastapi", "prefect", "sqlalchemy", "alembic", "uvicorn"):
        assert package in server
    assert "heliopuddle[cli]" in extras["server"]
    assert {_name(d) for d in extras["cli"]} == {"typer"}
    assert "server" in extras["all"][0]
    assert "cli" in extras["all"][0]


def _run_blocked(statement: str, allowed: tuple[str, ...] = ()) -> None:
    forbidden = tuple(p for p in SERVER_ONLY if p not in allowed)
    code = textwrap.dedent(
        f"""
        import importlib.abc, sys

        FORBIDDEN = {forbidden!r}

        class Block(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path=None, target=None):
                if name.split(".")[0] in FORBIDDEN:
                    raise ImportError(f"imported {{name}}")
                return None

        sys.meta_path.insert(0, Block())
        {statement}
        print("ok")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


@pytest.mark.req("DEP-17", "CLI-14")
def test_test_harness_needs_only_the_client_install() -> None:
    _run_blocked("from heliopuddle.testing import FakeHelioPuddleClient; FakeHelioPuddleClient()")


@pytest.mark.req("DEP-17", "CLI-15")
def test_cli_needs_only_the_cli_extra() -> None:
    _run_blocked("from heliopuddle.cli import app", allowed=("typer",))


@pytest.mark.req("DEP-17")
def test_client_imports_without_any_server_only_package() -> None:
    code = textwrap.dedent(
        f"""
        import importlib.abc, sys

        FORBIDDEN = {SERVER_ONLY!r}

        class Block(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path=None, target=None):
                if name.split(".")[0] in FORBIDDEN:
                    raise ImportError(f"the client imported {{name}}")
                return None

        sys.meta_path.insert(0, Block())
        from heliopuddle.client import HelioPuddleClient

        HelioPuddleClient("http://localhost:1/api/v1", "demo").close()
        print("ok")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def _bounds(requirements: list[str]) -> dict[str, tuple[int, ...]]:
    out = {}
    for req in requirements:
        match = re.match(r"([A-Za-z0-9_.-]+)(?:\[[^\]]*\])?\s*>=\s*([0-9][^,;\s]*)", req)
        if match:
            out[match[1].lower()] = tuple(int(p) for p in re.findall(r"\d+", match[2]))
    return out


@pytest.mark.req("DEP-18")
def test_client_floors_are_old_and_the_server_does_not_repeat_them() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    extras = project["optional-dependencies"]
    core = _bounds(project["dependencies"])
    server = _bounds(extras["server"])
    cli = _bounds(extras["cli"])

    # Floors wide enough for applications with their own pins.
    assert core["pydantic"] <= (2, 5, 0)
    assert core["httpx"] <= (0, 24, 0)
    assert core["fsspec"] <= (2023, 1, 0)
    assert core["pydantic-settings"] <= (2, 1, 0)
    assert cli["typer"] <= (0, 13, 0)
    # Bounds only where needed (DEP-09): the server extra does not re-declare them.
    for package in [*core, *cli]:
        assert package not in server, package


@pytest.mark.req("DEP-18")
def test_floor_script_pins_every_client_dependency() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("client_floor", ROOT / "scripts/client_floor.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    pins = module.floor_pins()
    assert {p.split("==")[0].lower() for p in pins} == CLIENT_DEPENDENCIES | {"typer"}
    assert "pydantic==2.5.0" in pins

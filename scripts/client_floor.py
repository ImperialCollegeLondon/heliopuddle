"""Run tests/client against the client's dependency floors (DEP-18).

Reads the lower bounds of the core dependencies and the ``cli`` extra from
``pyproject.toml``, installs exactly those versions (plus pytest) into a fresh
virtual environment, installs heliopuddle itself without dependencies, and runs
``tests/client`` there. The tests' conftest files above ``tests/client`` are
not loaded (they need the server).

Usage:
    uv run python scripts/client_floor.py                 # Python 3.11
    uv run python scripts/client_floor.py --python 3.12
    uv run python scripts/client_floor.py --print         # just the pins
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_REQ = re.compile(r"^\s*([A-Za-z0-9_.-]+)(?:\[[^\]]*\])?\s*>=\s*([0-9][^,;\s]*)")


def floor_pins() -> list[str]:
    """Return ``name==floor`` for every core dependency and the ``cli`` extra."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    pins = []
    for req in [*project["dependencies"], *project["optional-dependencies"]["cli"]]:
        match = _REQ.match(req)
        if match is None:
            raise SystemExit(f"no lower bound in {req!r}: every client dependency needs one")
        pins.append(f"{match[1]}=={match[2]}")
    return pins


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--python", default="3.11", help="Python version for the venv")
    parser.add_argument("--print", action="store_true", help="print the pins and exit")
    args = parser.parse_args(argv)

    pins = floor_pins()
    if args.print:
        print("\n".join(pins))
        return 0

    with tempfile.TemporaryDirectory(prefix="hp-floor-") as tmp:
        venv = Path(tmp) / "venv"
        python = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")

        def run(*cmd: str | Path) -> None:
            print("+", " ".join(str(c) for c in cmd), flush=True)
            subprocess.run([str(c) for c in cmd], check=True, cwd=ROOT)

        run("uv", "venv", "--python", args.python, venv)
        run("uv", "pip", "install", "--python", python, *pins, "pytest")
        run("uv", "pip", "install", "--python", python, "--no-deps", ".")
        result = subprocess.run(
            [
                str(python),
                "-m",
                "pytest",
                "tests/client",
                "-p",
                "no:cacheprovider",
                "--confcutdir=tests/client",
                # The project's ini options name server-only warning classes.
                "-o",
                "filterwarnings=",
                "-o",
                "asyncio_mode=strict",
                "-o",
                "asyncio_default_fixture_loop_scope=function",
            ],
            cwd=ROOT,
        )
        return result.returncode


if __name__ == "__main__":
    sys.exit(main())

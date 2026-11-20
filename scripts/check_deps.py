"""Check that the lockfile is current (DEP-09).

For every direct dependency (core, extras and the dev group) this prints the
lower bound declared in ``pyproject.toml`` (for information: bounds are loose
on purpose), the locked version and the latest release on PyPI. It fails when
a locked dependency is behind its latest release without a documented hold.

Usage:
    uv run python scripts/check_deps.py            # needs network access to PyPI
    uv run python scripts/check_deps.py --offline  # list bounds and locked versions

Run it after ``uv lock --upgrade`` at the start of every milestone and before
any large PR (see CLAUDE.md, "Dependencies").
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

HOLDS: dict[str, str] = {
    "fastapi": "capped by prefect 3.9 (fastapi<0.151)",
    "starlette": "follows fastapi, which prefect caps",
}
"""Dependencies allowed to lag their latest release, with the reason."""

_REQ = re.compile(r"^\s*([A-Za-z0-9_.-]+)(?:\[[^\]]*\])?\s*(.*)$")


def _normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def direct_requirements() -> dict[str, str | None]:
    """Return {name: declared lower bound or None} for every direct requirement."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    reqs: list[str] = list(project["project"]["dependencies"])
    for extra in project["project"].get("optional-dependencies", {}).values():
        reqs.extend(extra)
    reqs.extend(project.get("dependency-groups", {}).get("dev", []))
    out: dict[str, str | None] = {}
    for req in reqs:
        m = _REQ.match(req)
        if m is None or _normalise(m[1]) == "heliopuddle":
            continue
        lower = re.search(r">=\s*([0-9][^,;\s]*)", m[2])
        out.setdefault(_normalise(m[1]), lower[1] if lower else None)
    return out


def locked_versions() -> dict[str, str]:
    """Return {name: version} from uv.lock."""
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    return {_normalise(p["name"]): p["version"] for p in lock["package"] if "version" in p}


def latest_version(name: str) -> str:
    """Return the latest release of ``name`` on PyPI."""
    with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/json", timeout=30) as r:
        return json.load(r)["info"]["version"]


def _key(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", version))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--offline", action="store_true", help="do not ask PyPI")
    args = parser.parse_args(argv)

    locked = locked_versions()
    problems: list[str] = []
    print(f"{'package':<22} {'bound':<12} {'locked':<12} {'latest':<12} note")
    for name, bound in sorted(direct_requirements().items()):
        have = locked.get(name, "?")
        latest = "-" if args.offline else latest_version(name)
        note = ""
        if have == "?":
            note = "not locked"
            problems.append(f"{name}: not in uv.lock")
        elif latest != "-" and have != "?" and _key(have) < _key(latest):
            if name in HOLDS:
                note = f"held: {HOLDS[name]}"
            else:
                note = "BEHIND"
                problems.append(f"{name}: locked {have} < latest {latest}")
        print(f"{name:<22} {bound or '-':<12} {have:<12} {latest:<12} {note}")
    if problems:
        print("\nNOT OK:\n  " + "\n  ".join(problems))
        print("Run `uv lock --upgrade` (raising a bound only if it blocks), or document a hold.")
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

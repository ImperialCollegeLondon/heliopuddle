"""Print the requirement -> test -> result matrix and gate on the current milestone.

Usage:
    uv run pytest tests/unit --req-report=.reports/req-unit.json
    uv run pytest tests/integration --req-report=.reports/req-integration.json
    uv run python scripts/req_matrix.py [--milestone M1] [reports...]

With no report paths, every ``.reports/req-*.json`` is read. Exits 1 if any
requirement due in the milestone (or earlier) has no passing test, or any of
its tests failed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQ_RE = re.compile(
    r"^\*\*(?P<id>[A-Z]+-\d+[a-z]?) \((?P<ms>M\d+[A-Z]?)(?:, amended M\d+[A-Z]?)?\)\*\*\s*"
    r"(?P<text>.*)$",
    re.M,
)


def milestone_key(milestone: str) -> tuple[int, str]:
    """Order key for ``M5``, ``M5A``, ...: ``M5 < M5A < M6``."""
    m = re.fullmatch(r"M(\d+)([A-Z]?)", milestone.strip())
    if m is None:
        raise ValueError(f"invalid milestone {milestone!r}; use e.g. M4 or M5A")
    return (int(m[1]), m[2])


def load_requirements() -> dict[str, tuple[tuple[int, str], str]]:
    """Return {req id: (milestone key, first line of text)} from docs/spec/requirements."""
    reqs: dict[str, tuple[tuple[int, str], str]] = {}
    for f in sorted((ROOT / "docs/spec/requirements").glob("*.md")):
        for m in REQ_RE.finditer(f.read_text()):
            reqs[m["id"]] = (milestone_key(m["ms"]), m["text"].strip())
    return reqs


def load_results(paths: list[Path]) -> dict[str, dict[str, str]]:
    """Merge report files into {req id: {test node id: outcome}}."""
    merged: dict[str, dict[str, str]] = {}
    for p in paths:
        for rid, tests in json.loads(p.read_text()).items():
            merged.setdefault(rid, {}).update(tests)
    return merged


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("reports", nargs="*", type=Path)
    parser.add_argument("--milestone", default=None)
    args = parser.parse_args(argv)

    milestone = (
        args.milestone
        or tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["heliopuddle"]["milestone"]
    )
    due = milestone_key(milestone)
    reports = args.reports or sorted((ROOT / ".reports").glob("req-*.json"))
    results = load_results(reports)
    reqs = load_requirements()

    ok = True
    print(f"Requirement matrix for {milestone} ({len(reports)} report file(s))\n")
    print(f"{'REQ':<8} {'MS':<4} {'RESULT':<8} TESTS")
    for rid, (ms, _text) in sorted(reqs.items(), key=lambda kv: (kv[1][0], kv[0])):
        if ms > due:
            continue
        tests = results.get(rid, {})
        passed = [t for t, o in tests.items() if o == "passed"]
        failed = [t for t, o in tests.items() if o == "failed"]
        status = "FAIL" if failed else ("PASS" if passed else "MISSING")
        if status != "PASS":
            ok = False
        print(f"{rid:<8} M{ms[0]}{ms[1]:<3} {status:<8} {len(passed)} passed, {len(failed)} failed")
        for t in sorted(tests):
            print(f"{'':<22}{tests[t]:<8} {t}")
    unknown = sorted(set(results) - set(reqs))
    if unknown:
        ok = False
        print(f"\nTests reference unknown requirement IDs: {', '.join(unknown)}")
    print("\nOK" if ok else "\nNOT OK: requirements due without a passing test")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

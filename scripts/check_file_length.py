"""Fail if any Python file in the repository is longer than the line limit.

Long modules are split into smaller, better encapsulated ones (CLAUDE.md
"Code size"). Run: ``uv run python scripts/check_file_length.py``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECKED_DIRS = ("src", "tests", "scripts", "examples", "docker")
MAX_LINES = 500


def long_files(root: Path, max_lines: int) -> list[tuple[Path, int]]:
    """Return the Python files under the checked directories over ``max_lines``.

    Args:
        root: the repository root.
        max_lines: the largest allowed number of lines per file.

    Returns:
        ``(path, line count)`` pairs, longest first.
    """
    found: list[tuple[Path, int]] = []
    for name in CHECKED_DIRS:
        for path in (root / name).rglob("*.py"):
            if ".venv" in path.parts:
                continue
            count = len(path.read_text(encoding="utf-8").splitlines())
            if count > max_lines:
                found.append((path.relative_to(root), count))
    return sorted(found, key=lambda item: -item[1])


def main(argv: list[str] | None = None) -> int:
    """Print the files over the limit and return a non-zero exit code if any."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-lines", type=int, default=MAX_LINES)
    args = parser.parse_args(argv)
    found = long_files(ROOT, args.max_lines)
    for path, count in found:
        print(f"{path}: {count} lines (limit {args.max_lines})")
    if found:
        print(f"{len(found)} file(s) over {args.max_lines} lines; split them up.")
        return 1
    print(f"All Python files are at most {args.max_lines} lines.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

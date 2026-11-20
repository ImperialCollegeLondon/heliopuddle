"""Checks that the heliopuddle image holds no test tooling or test files (DEP-16).

Not copied into the image (``docker/`` is in .dockerignore): it is piped in by
the e2e tests and the Docker publish workflow:

    docker run --rm -i --entrypoint python IMAGE - < docker/image_checks.py

Exits non-zero, listing what it found, if a test package is importable or a
test file ships inside the installed ``heliopuddle`` package.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

TEST_PACKAGES = ("pytest", "playwright", "testcontainers", "hypothesis", "coverage", "_pytest")


def problems() -> list[str]:
    """What test tooling or test files the image contains."""
    found = [f"importable: {name}" for name in TEST_PACKAGES if importlib.util.find_spec(name)]
    spec = importlib.util.find_spec("heliopuddle")
    if spec is None or spec.origin is None:
        return [*found, "heliopuddle is not installed"]
    package = Path(spec.origin).parent
    for path in package.rglob("*"):
        if path.name == "tests" or path.name.startswith("test_") or path.name == "conftest.py":
            found.append(f"test file: {path}")
    return found


if __name__ == "__main__":
    issues = problems()
    for issue in issues:
        print(issue)
    print("image checks:", "FAILED" if issues else "ok")
    sys.exit(1 if issues else 0)

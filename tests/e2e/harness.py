"""Helpers for the e2e suite: run docker commands, describe the running container."""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPORTS = ROOT / ".reports"


def docker(*args: str, check: bool = True, timeout: float = 120) -> str:
    """Run ``docker *args`` and return its stdout; raise on failure when ``check``."""
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"docker {shlex.join(args)} failed ({result.returncode}):\n{result.stderr}"
        )
    return result.stdout.strip()


def docker_logs(container: str) -> str:
    """Return the container's combined stdout and stderr logs."""
    result = subprocess.run(
        ["docker", "logs", container], capture_output=True, text=True, timeout=60, check=False
    )
    return result.stdout + result.stderr


@dataclass(frozen=True)
class HpContainer:
    """A running heliopuddle container."""

    id: str
    base_url: str
    data_dir: Path
    project: str

    @property
    def api_url(self) -> str:
        """Base URL of the API, including ``/api/v1``."""
        return f"{self.base_url}/api/v1"

    def exec(self, *cmd: str) -> str:
        """Run a command in the container and return its stdout."""
        return docker("exec", self.id, *cmd)

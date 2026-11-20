"""Entry point of the embedded Prefect server child process.

Runs the Prefect CLI (``python -m heliopuddle.server.services._prefect_child
server start ...``). On Linux it first asks the kernel to send it SIGTERM when
its parent dies, so the server never outlives a killed heliopuddle process.
"""

from __future__ import annotations

import ctypes
import os
import signal
import sys

PR_SET_PDEATHSIG = 1


def _die_with_parent() -> None:  # pragma: no cover - runs in the child process
    if not sys.platform.startswith("linux"):
        return
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
    except (OSError, AttributeError):
        return
    if os.getppid() == 1:  # the parent died before prctl
        sys.exit(1)


def main() -> None:  # pragma: no cover - runs in the child process
    """Run ``prefect`` with this process's arguments."""
    _die_with_parent()
    from prefect.cli import app

    sys.argv[0] = "prefect"
    app()


if __name__ == "__main__":  # pragma: no cover
    main()

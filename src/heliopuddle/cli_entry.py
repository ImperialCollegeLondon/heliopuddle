"""The ``heliopuddle`` console script.

The command line needs the ``cli`` extra (typer), but the script is installed
with the core package too (DEP-17). Without the extra, say how to install it
instead of failing with a traceback.
"""

from __future__ import annotations

import sys

INSTALL_HINT = (
    "heliopuddle: the command line needs the cli extra, which is not installed.\n"
    'Install it with: pip install "heliopuddle[cli]"\n'
    '(or "heliopuddle[server]" to also run a server).'
)


def main() -> None:
    """Run the heliopuddle CLI, or exit 1 with an install hint without typer."""
    try:
        import typer  # noqa: F401  # pyright: ignore[reportUnusedImport]
    except ModuleNotFoundError as error:
        if error.name != "typer":
            raise
        print(INSTALL_HINT, file=sys.stderr)
        raise SystemExit(1) from None

    from heliopuddle.cli import app

    app()

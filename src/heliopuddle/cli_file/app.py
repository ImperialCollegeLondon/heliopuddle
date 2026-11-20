"""The ``file`` typer app; the command modules register themselves on it."""

from __future__ import annotations

import typer

file_app = typer.Typer(
    no_args_is_help=True,
    help=(
        "Add, register, list, show, update, download and remove files in a project through "
        "the API. "
        "Choose the project with --project (HELIOPUDDLE_PROJECT)."
    ),
)

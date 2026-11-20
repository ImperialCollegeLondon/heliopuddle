"""Shared typer options and arguments of the ``file`` commands."""

from __future__ import annotations

from typing import Annotated

import typer

_PANEL = "Connection and credentials"

ApiUrl = Annotated[
    str,
    typer.Option(
        "--api-url", envvar="HELIOPUDDLE_API_URL", help="API base URL.", rich_help_panel=_PANEL
    ),
]
Project = Annotated[
    str,
    typer.Option(
        "--project", "-p", envvar="HELIOPUDDLE_PROJECT", help="Project.", rich_help_panel=_PANEL
    ),
]
Token = Annotated[
    str | None,
    typer.Option(
        "--token", envvar="HELIOPUDDLE_TOKEN", help="Bearer token.", rich_help_panel=_PANEL
    ),
]
Username = Annotated[
    str | None,
    typer.Option(
        "--username",
        envvar="HELIOPUDDLE_USERNAME",
        help="Account to log in as; its password comes from --password-stdin, "
        "HELIOPUDDLE_PASSWORD or a prompt.",
        rich_help_panel=_PANEL,
    ),
]
PasswordStdin = Annotated[
    bool,
    typer.Option(
        "--password-stdin",
        help="Read the password from the first line of stdin.",
        rich_help_panel=_PANEL,
    ),
]
User = Annotated[
    str | None,
    typer.Option(
        "--user",
        envvar="HELIOPUDDLE_USER",
        help="User id for servers in header auth mode (no login).",
        rich_help_panel=_PANEL,
    ),
]
AsJson = Annotated[bool, typer.Option("--json", help="Print the record(s) as JSON.")]
Location = Annotated[
    str | None,
    typer.Option("--storage-location", "-s", help="Storage location; default the project's first."),
]
FileId = Annotated[int | None, typer.Argument(help="File id.", show_default=False)]
FileIds = Annotated[list[int] | None, typer.Argument(help="File ids.", show_default=False)]
VersionNumber = Annotated[
    int | None,
    typer.Option(
        "--version-number",
        help="Number version for a new file, instead of the one from the path.",
        min=0,
        show_default=False,
    ),
]
VersionText = Annotated[
    str | None,
    typer.Option(
        "--version-text",
        help="Text version for a new file, instead of the one from the path.",
        show_default=False,
    ),
]
PathGlob = Annotated[
    str | None,
    typer.Option(
        "--path",
        help="Live files whose path matches this glob (* within a folder, ** any depth, "
        "? one character); a plain path matches just that file.",
    ),
]
NameGlob = Annotated[
    str | None,
    typer.Option("--name", help="Live files whose file name (any folder) matches this glob."),
]

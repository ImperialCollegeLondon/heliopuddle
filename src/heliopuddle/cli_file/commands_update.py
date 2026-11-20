"""``file update``: change the metadata and content date of live files."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any

import typer

from heliopuddle.cli_file.app import file_app
from heliopuddle.cli_file.format import _metadata, _print
from heliopuddle.cli_file.options import (
    ApiUrl,
    AsJson,
    FileIds,
    NameGlob,
    PasswordStdin,
    PathGlob,
    Project,
    Token,
    User,
    Username,
)
from heliopuddle.cli_file.select import _select
from heliopuddle.cli_file.session import _session
from heliopuddle.cli_user import DEFAULT_API_URL, _fail


def _metadata_patch(
    metadata: str | None, set_: list[str] | None, unset: list[str] | None
) -> dict[str, Any] | None:
    """The metadata patch from --metadata, --set KEY=VALUE and --unset KEY (None: no change)."""
    if metadata is None and not set_ and not unset:
        return None
    patch: dict[str, Any] = dict(_metadata(metadata) or {})
    for item in set_ or []:
        key, sep, raw = item.partition("=")
        if not sep or not key:
            _fail(f"--set takes KEY=VALUE, not {item!r}", 2)
        try:
            patch[key] = json.loads(raw)
        except json.JSONDecodeError:
            patch[key] = raw  # not JSON: a plain string
    for key in unset or []:
        patch[key] = None
    return patch


@file_app.command("update")
def update(
    ids: FileIds = None,
    path: PathGlob = None,
    name: NameGlob = None,
    set_: Annotated[
        list[str] | None,
        typer.Option(
            "--set",
            help="Set a metadata key: KEY=VALUE (VALUE is JSON if it parses, else a string). "
            "Repeatable.",
        ),
    ] = None,
    unset: Annotated[
        list[str] | None, typer.Option("--unset", help="Remove a metadata key. Repeatable.")
    ] = None,
    metadata: Annotated[
        str | None,
        typer.Option("--metadata", "-m", help="JSON object merged into the metadata."),
    ] = None,
    replace_metadata: Annotated[
        bool,
        typer.Option(
            "--replace-metadata",
            help="Replace the whole metadata with --metadata/--set instead of merging.",
        ),
    ] = False,
    content_date: Annotated[
        datetime | None, typer.Option("--content-date", help="Set the content date (UTC).")
    ] = None,
    clear_content_date: Annotated[
        bool,
        typer.Option(
            "--clear-content-date",
            help="Go back to the date in the path (a version rule's date group), or none.",
        ),
    ] = False,
    as_json: AsJson = False,
    api_url: ApiUrl = DEFAULT_API_URL,
    project: Project = "default",
    token: Token = None,
    username: Username = None,
    password_stdin: PasswordStdin = False,
    user: User = None,
) -> None:
    """Change the metadata and content date of live files.

    The version is not set here: it comes from the path, through the project's
    version rules.
    """
    patch = _metadata_patch(metadata, set_, unset)
    if content_date is not None and clear_content_date:
        _fail("give --content-date or --clear-content-date, not both", 2)
    if replace_metadata and patch is None:
        _fail("--replace-metadata needs --metadata or --set", 2)
    if patch is None and content_date is None and not clear_content_date:
        _fail("nothing to change: give --set, --unset, --metadata or a content date option", 2)
    with _session(api_url, project, token, username, password_stdin, user) as client:
        records = []
        for record in _select(client, ids, path, name):
            if patch is not None:
                record = client.update_metadata(record.id, patch, replace=replace_metadata)
            if content_date is not None or clear_content_date:
                record = client.set_content_date(record.id, content_date)
            records.append(record)
    _print(records, as_json, many=len(records) > 1 or path is not None or name is not None)

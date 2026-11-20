"""``file list``, ``file show`` and ``file download``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from heliopuddle.cli_file.app import file_app
from heliopuddle.cli_file.format import _copy_cell, _print, _version_label
from heliopuddle.cli_file.options import (
    ApiUrl,
    AsJson,
    FileId,
    NameGlob,
    PasswordStdin,
    PathGlob,
    Project,
    Token,
    User,
    Username,
)
from heliopuddle.cli_file.select import _select_one
from heliopuddle.cli_file.session import _session
from heliopuddle.cli_user import DEFAULT_API_URL, _table
from heliopuddle.client import FileRecord, HelioPuddleClient


@file_app.command("list")
def list_(
    pattern: Annotated[
        str | None,
        typer.Argument(help="Path glob: * within a folder, ** any depth, ? one character."),
    ] = None,
    latest: Annotated[
        bool, typer.Option("--latest", help="Only the latest version of each series.")
    ] = False,
    deleted: Annotated[bool, typer.Option("--deleted", help="Include deleted files.")] = False,
    limit: Annotated[int, typer.Option("--limit", "-n", help="At most this many.", min=1)] = 100,
    as_json: AsJson = False,
    api_url: ApiUrl = DEFAULT_API_URL,
    project: Project = "default",
    token: Token = None,
    username: Username = None,
    password_stdin: PasswordStdin = False,
    user: User = None,
) -> None:
    """List files, ordered by path."""
    with _session(api_url, project, token, username, password_stdin, user) as client:
        records: list[FileRecord] = []
        for record in client.find_all(
            path=pattern,
            latest_only=latest,
            include_deleted=deleted,
            order="path",
            page_size=min(limit, 1000),
        ):
            records.append(record)
            if len(records) >= limit:
                break
        if as_json:
            _print(records, True, many=True)
            return
        if not records:
            typer.echo("No files.")
            return
        locations = _location_columns(client, records)
    rows = [
        [
            str(r.id),
            *(_copy_cell(r, name) for name in locations),
            str(r.size),
            r.updated_at.strftime("%Y-%m-%d %H:%M"),
            r.path + (" (deleted)" if r.deleted_at else ""),
        ]
        for r in records
    ]
    headers = ["ID", *(name.upper() for name in locations), "SIZE", "UPDATED", "PATH"]
    typer.echo(_table(rows, headers))


def _location_columns(client: HelioPuddleClient, records: list[FileRecord]) -> list[str]:
    """Storage locations to show as columns: the active ones in config order, plus
    any inactive one that still holds a listed copy."""
    used = {c.storage_location for r in records for c in r.storage_locations}
    return [sl.name for sl in client.list_storage_locations() if sl.active or sl.name in used]


@file_app.command("show")
def show(
    file_id: FileId = None,
    path: PathGlob = None,
    name: NameGlob = None,
    as_json: AsJson = False,
    api_url: ApiUrl = DEFAULT_API_URL,
    project: Project = "default",
    token: Token = None,
    username: Username = None,
    password_stdin: PasswordStdin = False,
    user: User = None,
) -> None:
    """Show one file: hash, size, version, metadata and its copies."""
    with _session(api_url, project, token, username, password_stdin, user) as client:
        record = _select_one(client, file_id, path, name)
    if as_json:
        _print([record], True)
        return
    lines = [
        f"id:            {record.id}",
        f"path:          {record.path}",
        f"md5:           {record.hash}",
        f"size:          {record.size}",
        f"version:       {_version_label(record)}",
        f"latest:        {'yes' if record.is_latest else 'no'}",
        f"content date:  {record.content_date or '-'}",
        f"created:       {record.created_at} by {record.created_by or '-'}",
        f"deleted:       {record.deleted_at or 'no'}",
        f"metadata:      {json.dumps(record.metadata) if record.metadata else '-'}",
        "copies:",
        *(f"  {c.storage_location}: {c.state}" for c in record.storage_locations),
    ]
    typer.echo("\n".join(lines))


@file_app.command("download")
def download(
    file_id: FileId = None,
    path: PathGlob = None,
    name: NameGlob = None,
    dest: Annotated[
        Path | None,
        typer.Option("--to", "-o", help="Local file or directory; default the file name here."),
    ] = None,
    api_url: ApiUrl = DEFAULT_API_URL,
    project: Project = "default",
    token: Token = None,
    username: Username = None,
    password_stdin: PasswordStdin = False,
    user: User = None,
) -> None:
    """Download a file and check its md5."""
    with _session(api_url, project, token, username, password_stdin, user) as client:
        record = _select_one(client, file_id, path, name)
        target = dest if dest is not None else Path(record.name)
        if target.is_dir():
            target = target / record.name
        written = client.download(record.id, target)
    typer.echo(str(written))

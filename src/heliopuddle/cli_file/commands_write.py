"""``file add``, ``file register`` and ``file remove``."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer

from heliopuddle.cli_file.app import file_app
from heliopuddle.cli_file.format import (
    _check_version_options,
    _incomplete,
    _join,
    _metadata,
    _print,
)
from heliopuddle.cli_file.options import (
    ApiUrl,
    AsJson,
    FileIds,
    Location,
    NameGlob,
    PasswordStdin,
    PathGlob,
    Project,
    Token,
    User,
    Username,
    VersionNumber,
    VersionText,
)
from heliopuddle.cli_file.select import _select
from heliopuddle.cli_file.session import _session
from heliopuddle.cli_user import DEFAULT_API_URL, _fail
from heliopuddle.client import FileRecord


@file_app.command("add")
def add(
    local: Annotated[Path, typer.Argument(help="Local file, or a directory with --recursive.")],
    path: Annotated[
        str | None,
        typer.Argument(
            help="Destination path in the store (a prefix with --recursive). "
            "Default: the local file name."
        ),
    ] = None,
    recursive: Annotated[
        bool, typer.Option("--recursive", "-r", help="Add every file under a directory.")
    ] = False,
    overwrite: Annotated[
        bool, typer.Option("--overwrite", help="Replace a live file with different content.")
    ] = False,
    metadata: Annotated[
        str | None, typer.Option("--metadata", "-m", help="JSON object stored with the file.")
    ] = None,
    content_date: Annotated[
        datetime | None, typer.Option("--content-date", help="Content date (UTC).")
    ] = None,
    storage_location: Location = None,
    version_number: VersionNumber = None,
    version_text: VersionText = None,
    as_json: AsJson = False,
    api_url: ApiUrl = DEFAULT_API_URL,
    project: Project = "default",
    token: Token = None,
    username: Username = None,
    password_stdin: PasswordStdin = False,
    user: User = None,
) -> None:
    """Upload a file (or a directory tree) and register it; identical content is a no-op.

    --version-number or --version-text sets the version of a single new file;
    an existing file keeps its version.
    """
    meta = _metadata(metadata)
    _check_version_options(version_number, version_text)
    versioned = version_number is not None or version_text is not None
    if versioned and (recursive or local.is_dir()):
        _fail("--version-number and --version-text take a single file, not a directory", 2)
    if local.is_dir():
        if not recursive:
            _fail(f"{local} is a directory; use --recursive", 2)
        prefix = path if path is not None else local.name
        sources = sorted(p for p in local.rglob("*") if p.is_file())
        targets = [_join(prefix, p.relative_to(local).as_posix()) for p in sources]
    else:
        sources, targets = [local], [path if path is not None else local.name]
    records: list[FileRecord] = []
    with _session(
        api_url,
        project,
        token,
        username,
        password_stdin,
        user,
        version=(version_number, version_text),
    ) as client:
        for source, target in zip(sources, targets, strict=True):
            records.append(
                client.add_file(
                    source,
                    target,
                    storage_location=storage_location,
                    metadata=meta,
                    overwrite=overwrite,
                    content_date=content_date,
                    version_number=version_number,
                    version_text=version_text,
                )
            )
    _print(records, as_json, many=recursive)


@file_app.command("register")
def register(
    path: Annotated[str, typer.Argument(help="Path of an object already in the store.")],
    hash: Annotated[str, typer.Option("--hash", help="Hex md5 of the content.")],
    size: Annotated[int, typer.Option("--size", help="Size in bytes.", min=0)],
    metadata: Annotated[
        str | None, typer.Option("--metadata", "-m", help="JSON object stored with the file.")
    ] = None,
    content_date: Annotated[
        datetime | None, typer.Option("--content-date", help="Content date (UTC).")
    ] = None,
    storage_location: Location = None,
    version_number: VersionNumber = None,
    version_text: VersionText = None,
    as_json: AsJson = False,
    api_url: ApiUrl = DEFAULT_API_URL,
    project: Project = "default",
    token: Token = None,
    username: Username = None,
    password_stdin: PasswordStdin = False,
    user: User = None,
) -> None:
    """Register a file another tool has already written to the store (nothing is uploaded)."""
    meta = _metadata(metadata)
    _check_version_options(version_number, version_text)
    with _session(
        api_url,
        project,
        token,
        username,
        password_stdin,
        user,
        version=(version_number, version_text),
    ) as client:
        record = client.register_file(
            path,
            hash,
            size,
            storage_location=storage_location,
            metadata=meta,
            content_date=content_date,
            version_number=version_number,
            version_text=version_text,
        )
    _print([record], as_json)


@file_app.command("remove")
def remove(
    ids: FileIds = None,
    path: PathGlob = None,
    name: NameGlob = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")] = False,
    as_json: AsJson = False,
    api_url: ApiUrl = DEFAULT_API_URL,
    project: Project = "default",
    token: Token = None,
    username: Username = None,
    password_stdin: PasswordStdin = False,
    user: User = None,
) -> None:
    """Delete files from every storage location (the records are kept as deleted).

    Asks first, listing the chosen files, unless --yes. Credentials are read
    before the question, so --password-stdin takes the first line of stdin.
    """
    many = False
    with _session(api_url, project, token, username, password_stdin, user) as client:
        targets = _select(client, ids, path, name)
        many = len(targets) > 1 or path is not None or name is not None
        if not yes:
            listing = "\n".join(f"  {r.id}\t{r.path}" for r in targets[:20])
            more = f"\n  ... and {len(targets) - 20} more" if len(targets) > 20 else ""
            if not typer.confirm(
                f"{listing}{more}\nDelete these {len(targets)} file(s) "
                "from every storage location?",
                default=False,
            ):
                _fail("aborted", 1)
        records = [client.remove_file(r.id) for r in targets]
    _print(records, as_json, many=many)
    incomplete = _incomplete(records)
    if incomplete:
        _fail("not deleted from every storage location:\n  " + "\n  ".join(incomplete))

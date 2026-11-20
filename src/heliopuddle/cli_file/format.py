"""Option checks and output formatting of the ``file`` commands."""

from __future__ import annotations

import json
from typing import Any

import typer
from pydantic import ValidationError

from heliopuddle.cli_user import _fail
from heliopuddle.client import FileRecord
from heliopuddle.server.schemas import FileState, VersionOverride


def _incomplete(records: list[FileRecord]) -> list[str]:
    """Why each file that is still live after a delete is not gone, one line each.

    A copy left ``deleting`` failed to delete and the index retries it; a copy
    marked ``unknown`` held content that changed since heliopuddle last saw
    it, so it was kept and needs review (``accept`` it, or remove it again).
    """
    lines = []
    for r in records:
        if r.deleted_at is not None:
            continue
        unknown = [c.storage_location for c in r.storage_locations if c.state == FileState.UNKNOWN]
        deleting = [
            c.storage_location for c in r.storage_locations if c.state == FileState.DELETING
        ]
        reasons = []
        if deleting:
            reasons.append(f"delete failed in {', '.join(deleting)} (the index retries)")
        if unknown:
            reasons.append(
                f"changed content kept in {', '.join(unknown)} (review it: accept or remove again)"
            )
        lines.append(f"{r.path}: {'; '.join(reasons) or 'still live'}")
    return lines


def _check_version_options(version_number: int | None, version_text: str | None) -> None:
    """Reject invalid version options (exit 2) before any request (CLI-15)."""
    if version_number is not None and version_text is not None:
        _fail("give either --version-number or --version-text, not both", 2)
    try:
        VersionOverride(version_number=version_number, version_text=version_text)
    except ValidationError as e:
        error = e.errors()[0]
        option = "--" + str(error["loc"][0]).replace("_", "-") if error["loc"] else "version"
        _fail(f"invalid {option}: {error['msg']}", 2)


def _version_label(record: FileRecord) -> str:
    """``v12`` for a number version, a text version as is, ``-`` for none."""
    if record.version_number is not None:
        return f"v{record.version_number}"
    return record.version_text if record.version_text is not None else "-"


def _metadata(value: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as e:
        _fail(f"--metadata is not valid JSON: {e.msg}", 2)
    if not isinstance(parsed, dict):
        _fail("--metadata must be a JSON object", 2)
    return parsed


def _print(records: list[FileRecord], as_json: bool, many: bool = False) -> None:
    if as_json:
        data = [r.model_dump(mode="json") for r in records]
        typer.echo(json.dumps(data if many else data[0], indent=2))
        return
    for r in records:
        typer.echo(f"{r.id}\t{_copies(r)}\t{r.path}")


def _copies(record: FileRecord) -> str:
    """The file's copies as ``location=state`` pairs; ``-`` when none is left."""
    pairs = [
        f"{c.storage_location}={c.state}"
        for c in record.storage_locations
        if c.state != FileState.DELETED
    ]
    return ",".join(pairs) or "-"


def _copy_cell(record: FileRecord, storage_location: str) -> str:
    """The state of the file's copy in ``storage_location``; ``-`` for none or a deleted one."""
    for c in record.storage_locations:
        if c.storage_location == storage_location and c.state != FileState.DELETED:
            return str(c.state)
    return "-"


def _join(prefix: str, relative: str) -> str:
    return f"{prefix.rstrip('/')}/{relative}" if prefix else relative

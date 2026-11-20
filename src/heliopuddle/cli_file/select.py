"""Choosing the files a command works on: ids, ``--path`` and ``--name``."""

from __future__ import annotations

from heliopuddle.cli_user import _fail
from heliopuddle.client import FileRecord, HelioPuddleClient, NotFoundError


def _select(
    client: HelioPuddleClient,
    ids: list[int] | None,
    path: str | None,
    name: str | None,
    *,
    live_only: bool = True,
) -> list[FileRecord]:
    """The files chosen by ids, ``--path`` and ``--name`` (at least one is needed).

    Every id must exist (and be live with ``live_only``); each glob must match
    at least one live file. Duplicates are dropped, first occurrence kept.
    """
    if not ids and path is None and name is None:
        _fail("give file ids, --path or --name", 2)
    if name is not None and "/" in name:
        _fail("--name matches a file name; use --path for folders", 2)
    chosen: dict[int, FileRecord] = {}
    for file_id in ids or []:
        record = client.get_file(file_id)
        if live_only and record.deleted_at is not None:
            raise NotFoundError(404, f"file {file_id} is deleted")
        chosen.setdefault(record.id, record)
    for option, pattern in (("--path", path), ("--name", f"**/{name}" if name else None)):
        if pattern is None:
            continue
        matches = list(client.find_all(pattern, order="path"))
        if not matches:
            shown = path if option == "--path" else name
            raise NotFoundError(404, f"no live file matches {option} {shown!r}")
        for record in matches:
            chosen.setdefault(record.id, record)
    return list(chosen.values())


def _select_one(
    client: HelioPuddleClient, file_id: int | None, path: str | None, name: str | None
) -> FileRecord:
    """Exactly one file, by id or by a ``--path``/``--name`` that matches one live file."""
    if sum(x is not None for x in (file_id, path, name)) > 1:
        _fail("give one of a file id, --path or --name", 2)
    records = _select(
        client, [file_id] if file_id is not None else None, path, name, live_only=False
    )
    if len(records) > 1:
        _fail(
            f"{len(records)} files match; narrow the glob or give an id: "
            + ", ".join(f"{r.id} {r.path}" for r in records[:10])
            + (" ..." if len(records) > 10 else "")
        )
    return records[0]

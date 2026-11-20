"""Per-file display helpers shared by the Files, file, Overview and search pages."""

from __future__ import annotations

from heliopuddle.server.schemas import FileRead, FileState
from heliopuddle.server.ui.shared.states import ELIGIBLE, IN_PROGRESS, READABLE


def version_label(file: FileRead) -> str:
    """A file's version for display.

    Args:
        file: the file.

    Returns:
        ``"v12"`` for a number version, a text version as it is (e.g.
        ``"001.0004"``), else ``"unversioned"``.
    """
    if file.version_number is not None:
        return f"v{file.version_number}"
    if file.version_text is not None:
        return file.version_text
    return "unversioned"


def latest_eligible(file: FileRead) -> bool:
    """Whether a file can be the latest of its series (API-12).

    Args:
        file: the file.

    Returns:
        True if at least one copy is ``pending`` or ``available``.
    """
    return any(c.state in ELIGIBLE for c in file.storage_locations)


def downloadable(file: FileRead) -> bool:
    """Whether a live file has a copy that serves its content (available or overwriting)."""
    return file.deleted_at is None and any(c.state in READABLE for c in file.storage_locations)


def file_health(file: FileRead) -> tuple[str, str]:
    """A one-phrase summary of a file's copies and the state class to colour it with.

    Returns:
        For example ``("1 copy missing", "missing")``, ``("in progress", "pending")``
        or ``("all copies available", "available")``.
    """
    states = [c.state for c in file.storage_locations]
    if file.deleted_at is not None:
        return "deleted", "none"
    for bad in (FileState.MISSING, FileState.UNKNOWN):
        count = states.count(bad)
        if count:
            return f"{count} {'copy' if count == 1 else 'copies'} {bad.value}", bad.value
    if any(s in IN_PROGRESS for s in states):
        return "in progress", "pending"
    if not states or all(s == FileState.DELETED for s in states):
        return "no copy", "none"
    return "all copies available", "available"

"""Errors raised by the file functions, and the version wording they share."""

from __future__ import annotations


class NotFoundError(LookupError):
    """A project, storage location, file, series or copy does not exist."""


class InactiveError(Exception):
    """The storage location is no longer in config (``active = false``)."""


class ConflictError(Exception):
    """A live file at the path has a different hash and ``overwrite`` was not set.

    Attributes:
        file_id: the existing live file.
    """

    def __init__(self, file_id: int, path: str) -> None:
        super().__init__(f"a different file already exists at {path!r}")
        self.file_id = file_id


def describe_version(version_number: int | None, version_text: str | None) -> str:
    """A version for messages: ``number 12``, ``text '001.0002'`` or ``no version``."""
    if version_number is not None:
        return f"number {version_number}"
    if version_text is not None:
        return f"text {version_text!r}"
    return "no version"


class VersionImmutable(Exception):
    """A version override differs from the live file's version (API-22, ``409``).

    Attributes:
        file_id: the existing live file.
    """

    def __init__(self, file_id: int, path: str, stored: str, requested: str) -> None:
        super().__init__(
            f"the file at {path!r} has {stored}; a version is set only when the file "
            f"is created, so {requested} cannot be applied"
        )
        self.file_id = file_id

"""Exceptions raised by :class:`heliopuddle.client.HelioPuddleClient`."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from heliopuddle.server.schemas import FileRead


class HelioPuddleError(Exception):
    """An API error.

    Attributes:
        status_code: the HTTP status code.
        detail: the server's ``detail`` (a string or a list of validation errors).
    """

    def __init__(self, status_code: int, detail: Any) -> None:
        super().__init__(f"{status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class NotFoundError(HelioPuddleError):
    """The API answered ``404``: unknown project, storage location or file."""


class AuthenticationError(HelioPuddleError):
    """The caller is not authenticated (AUTH-15).

    Raised for a ``401`` from the API that a fresh token did not fix, and for a
    failed token request (bad password, ``invalid_grant``, unreachable or
    unsupported token endpoint). The message never contains the password or
    the token.
    """


class PermissionDeniedError(HelioPuddleError):
    """The caller is authenticated but lacks a role (``403``, AUTH-15).

    Attributes:
        required_roles: roles of which the caller needs at least one (empty if
            the server did not say).
    """

    def __init__(self, detail: Any, required_roles: list[str] | None = None) -> None:
        super().__init__(403, detail)
        self.required_roles = list(required_roles or [])


class ConflictError(HelioPuddleError):
    """A live file with a different hash, or a different version, exists at the path (``409``).

    Raised by :meth:`HelioPuddleClient.add_file` when the reservation is
    refused for that reason (nothing has been written to the store), and by
    :meth:`HelioPuddleClient.register_file`.

    Attributes:
        file: the existing record at that path.
    """

    def __init__(self, detail: Any, file: FileRead) -> None:
        super().__init__(409, detail)
        self.file = file


class BusyError(HelioPuddleError):
    """The path is owned by another operation (``409`` with ``Retry-After``).

    Raised by :meth:`HelioPuddleClient.add_file` and
    :meth:`HelioPuddleClient.register_file` when an upload, overwrite or
    delete of the path is in progress; nothing has been written. Try again
    later.

    Attributes:
        retry_after: seconds the server asked to wait, or None if it gave no
            usable value.
    """

    def __init__(self, detail: Any, retry_after: float | None) -> None:
        super().__init__(409, detail)
        self.retry_after = retry_after


class IntegrityError(Exception):
    """Downloaded content does not match the hash in the record.

    Not an API error (the API answered fine; the store content is wrong), so
    it does not derive from :class:`HelioPuddleError`.

    Attributes:
        file: the record that was downloaded.
        expected: the hash in the record.
        actual: the md5 of the content read from the store.
    """

    def __init__(self, file: FileRead, actual: str) -> None:
        super().__init__(
            f"content of {file.path!r} (file {file.id}) does not match its record: "
            f"expected md5 {file.hash}, got {actual}"
        )
        self.file = file
        self.expected = file.hash
        self.actual = actual

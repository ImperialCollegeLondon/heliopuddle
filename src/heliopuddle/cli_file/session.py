"""Opening a client for a command and reporting API failures as exit codes."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import NoReturn

import httpx

from heliopuddle.cli_file.format import _version_label
from heliopuddle.cli_user import _describe_validation, _fail, _login_password
from heliopuddle.client import (
    AuthenticationError,
    BusyError,
    ConflictError,
    HelioPuddleClient,
    HelioPuddleError,
    IntegrityError,
    NotFoundError,
    PermissionDeniedError,
)


def _client_factory(
    api_url: str,
    project: str,
    *,
    user: str | None,
    token: str | None,
    username: str | None,
    password: str | None,
) -> HelioPuddleClient:
    """Build the API client; tests replace this to talk to an in-process API."""
    return HelioPuddleClient(
        api_url, project, user=user, token=token, username=username, password=password
    )


def _report(error: Exception, api_url: str, version: tuple[int | None, str | None]) -> NoReturn:
    """Turn an API or transfer failure into ``Error: ...`` and an exit code (never returns)."""
    if isinstance(error, ConflictError):
        if version != (None, None) and version != (
            error.file.version_number,
            error.file.version_text,
        ):
            _fail(
                f"{error.detail}. Existing file {error.file.id} has version "
                f"{_version_label(error.file)}; a file's version cannot be changed"
            )
        _fail(
            f"{error.detail}. Existing file {error.file.id} has md5 {error.file.hash}; "
            "use --overwrite to replace it"
        )
    if isinstance(error, BusyError):
        wait = f"; retry in {error.retry_after:.0f}s" if error.retry_after is not None else ""
        _fail(f"the path is busy with another operation{wait}")
    if isinstance(error, AuthenticationError):
        _fail(f"authentication failed: {error.detail}")
    if isinstance(error, PermissionDeniedError):
        roles = ", ".join(error.required_roles) or "a role"
        _fail(f"permission denied: one of {roles} is required")
    if isinstance(error, NotFoundError):
        _fail(f"not found: {error.detail}")
    if isinstance(error, HelioPuddleError):
        _fail(_describe_validation(error.detail) if error.status_code == 422 else str(error.detail))
    if isinstance(error, IntegrityError):
        _fail(str(error))
    if isinstance(error, FileNotFoundError):
        _fail(str(error), 2)
    _fail(f"cannot reach the API at {api_url}: {type(error).__name__}")


@contextmanager
def _session(
    api_url: str,
    project: str,
    token: str | None,
    username: str | None,
    password_stdin: bool,
    user: str | None,
    *,
    version: tuple[int | None, str | None] = (None, None),
) -> Iterator[HelioPuddleClient]:
    """Open a client and turn API failures into ``Error: ...`` and an exit code.

    ``version`` is the command's version override ``(number, text)``, if any:
    a conflict with a live file of another version cannot be fixed with
    ``--overwrite``, since a file's version never changes.
    """
    if token and username:
        _fail("give either --token or --username, not both", 2)
    if password_stdin and not username:
        _fail("--password-stdin goes with --username", 2)
    password = _login_password(password_stdin) if username else None
    client = _client_factory(
        api_url, project, user=user, token=token, username=username, password=password
    )
    try:
        yield client
    except (HelioPuddleError, IntegrityError, FileNotFoundError, httpx.HTTPError) as e:
        _report(e, api_url, version)
    finally:
        client.close()

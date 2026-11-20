"""``heliopuddle user ...``: manage local users through the API (AUTH-14).

This module is a thin layer over :class:`heliopuddle.client.HelioPuddleClient`
and so over the ``/users`` API (``local`` auth mode, ``hp-admin``). It never
touches the database (an import-linter contract enforces that) and never
accepts a password on the command line, where it would show up in the process
list and shell history.

Credentials (who runs the command): ``--token`` (env ``HELIOPUDDLE_TOKEN``), or
``--username`` (env ``HELIOPUDDLE_USERNAME``) with the password from
``--password-stdin``, env ``HELIOPUDDLE_PASSWORD`` or a hidden prompt.

A new user's password (``add``, ``set-password``) comes from ``--new-password-stdin``,
env ``HELIOPUDDLE_NEW_PASSWORD`` or a hidden prompt with confirmation.

Exit codes: 0 on success, 1 when the API refuses or cannot be reached, 2 for
invalid options.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import datetime
from typing import Annotated, Any, NoReturn

import httpx
import typer

from heliopuddle.client import (
    AuthenticationError,
    HelioPuddleClient,
    HelioPuddleError,
    NotFoundError,
    PermissionDeniedError,
    UserRead,
)

DEFAULT_API_URL = "http://127.0.0.1:8080/api/v1"
PASSWORD_ENV = "HELIOPUDDLE_PASSWORD"
NEW_PASSWORD_ENV = "HELIOPUDDLE_NEW_PASSWORD"

user_app = typer.Typer(
    no_args_is_help=True,
    help=(
        "Manage local users through the API (HELIOPUDDLE_AUTH_MODE=local; needs hp-admin). "
        "Say who you log in as with --username (HELIOPUDDLE_USERNAME), plus its password from "
        "a hidden prompt, --password-stdin or HELIOPUDDLE_PASSWORD, or use --token. "
        "The name after a command is the user being managed. Passwords are never accepted "
        "as options."
    ),
)

_AUTH_PANEL = "Connection and credentials"

ApiUrl = Annotated[
    str,
    typer.Option(
        "--api-url",
        envvar="HELIOPUDDLE_API_URL",
        help="API base URL (env HELIOPUDDLE_API_URL).",
        rich_help_panel=_AUTH_PANEL,
    ),
]
Token = Annotated[
    str | None,
    typer.Option(
        "--token",
        envvar="HELIOPUDDLE_TOKEN",
        help="Bearer token (env HELIOPUDDLE_TOKEN), instead of --username.",
        rich_help_panel=_AUTH_PANEL,
    ),
]
Username = Annotated[
    str | None,
    typer.Option(
        "--username",
        envvar="HELIOPUDDLE_USERNAME",
        help=(
            "The account to log in as, e.g. your admin (env HELIOPUDDLE_USERNAME); not the "
            "user being created or changed. Its password is read from --password-stdin, "
            "env HELIOPUDDLE_PASSWORD or a hidden prompt."
        ),
        rich_help_panel=_AUTH_PANEL,
    ),
]
PasswordStdin = Annotated[
    bool,
    typer.Option(
        "--password-stdin",
        help="Read YOUR password (for --username) from the first line of stdin.",
        rich_help_panel=_AUTH_PANEL,
    ),
]
NewPasswordStdin = Annotated[
    bool,
    typer.Option(
        "--new-password-stdin",
        help=(
            "Read the NEW password from the first line of stdin. Otherwise it comes from "
            f"env {NEW_PASSWORD_ENV} or a hidden prompt with confirmation."
        ),
    ),
]
Name = Annotated[str, typer.Argument(help="The username (case-insensitive).")]


def _client_factory(
    api_url: str, *, token: str | None, username: str | None, password: str | None
) -> HelioPuddleClient:
    """Build the API client; tests replace this to talk to an in-process API."""
    return HelioPuddleClient(api_url, None, token=token, username=username, password=password)


def _fail(message: str, code: int = 1) -> NoReturn:
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(code)


def _read_stdin_password() -> str:
    line = sys.stdin.readline()
    password = line.rstrip("\r\n")
    if not password:
        _fail("expected a password on the first line of stdin", 2)
    return password


def _login_password(password_stdin: bool) -> str:
    if password_stdin:
        return _read_stdin_password()
    from_env = os.environ.get(PASSWORD_ENV)
    if from_env:
        return from_env
    return typer.prompt("Password", hide_input=True)


def _new_password(new_password_stdin: bool, password_stdin: bool) -> str:
    if new_password_stdin:
        if password_stdin:
            _fail("--password-stdin and --new-password-stdin cannot both read stdin", 2)
        return _read_stdin_password()
    from_env = os.environ.get(NEW_PASSWORD_ENV)
    if from_env:
        return from_env
    return typer.prompt("New password", hide_input=True, confirmation_prompt=True)


def _open(
    api_url: str, token: str | None, username: str | None, password_stdin: bool
) -> HelioPuddleClient:
    if token and username:
        _fail("give either --token or --username, not both", 2)
    if token:
        if password_stdin:
            _fail("--password-stdin goes with --username, not --token", 2)
        return _client_factory(api_url, token=token, username=None, password=None)
    if not username:
        _fail(
            "give credentials: say which account you log in as with --username "
            "(HELIOPUDDLE_USERNAME, e.g. admin; not the user you are creating) and give its "
            f"password at the hidden prompt, with --password-stdin or in {PASSWORD_ENV}; "
            "or use --token (HELIOPUDDLE_TOKEN)",
            2,
        )
    password = _login_password(password_stdin)
    return _client_factory(api_url, token=None, username=username, password=password)


def _describe_validation(detail: Any) -> str:
    if not isinstance(detail, list):
        return str(detail)
    parts: list[str] = []
    for item in detail:
        if isinstance(item, dict):
            loc = ".".join(str(p) for p in item.get("loc", ()) if p != "body")
            msg = str(item.get("msg", "invalid")).removeprefix("Value error, ")
            parts.append(f"{loc}: {msg}" if loc else msg)
        else:
            parts.append(str(item))
    return "; ".join(parts)


def _not_found_message(client: HelioPuddleClient, error: NotFoundError, what: str | None) -> str:
    """A 404 is an unknown user in ``local`` mode, and "no user API" in any other mode."""
    mode: str | None = None
    with suppress(HelioPuddleError, httpx.HTTPError, ValueError):
        mode = client.auth_config().mode
    if mode is not None and mode != "local":
        return (
            f"users are managed elsewhere: the server runs in auth mode {mode!r}, which has "
            "no user API (only HELIOPUDDLE_AUTH_MODE=local does). Manage users in your "
            "identity provider."
        )
    if what is not None and mode == "local":
        return f"user {what!r} not found"
    if what is None:
        return (
            "users are managed elsewhere: the server has no user API "
            "(it needs HELIOPUDDLE_AUTH_MODE=local) or this URL is not a heliopuddle API."
        )
    return f"not found: {error.detail}"


@contextmanager
def _session(
    api_url: str,
    token: str | None,
    username: str | None,
    password_stdin: bool,
    *,
    what: str | None = None,
) -> Iterator[HelioPuddleClient]:
    """Open a client and turn API failures into ``Error: ...`` and an exit code.

    Args:
        api_url: the API base URL.
        token: a bearer token, or None.
        username: the login name, or None.
        password_stdin: read the login password from stdin.
        what: the username the command is about, for "not found" messages.
    """
    client = _open(api_url, token, username, password_stdin)
    try:
        yield client
    except AuthenticationError as e:
        _fail(f"authentication failed: {e.detail}")
    except PermissionDeniedError as e:
        needed = ", ".join(e.required_roles) or "hp-admin"
        _fail(f"permission denied: one of {needed} is required")
    except NotFoundError as e:
        _fail(_not_found_message(client, e, what))
    except HelioPuddleError as e:
        if e.status_code == 422:
            _fail(_describe_validation(e.detail))
        _fail(str(e.detail))
    except httpx.HTTPError as e:
        _fail(f"cannot reach the API at {api_url}: {type(e).__name__}")
    finally:
        client.close()


def _split_roles(value: str) -> list[str]:
    return [r.strip() for r in value.split(",") if r.strip()]


def _when(value: datetime | None) -> str:
    return "never" if value is None else value.strftime("%Y-%m-%d %H:%M:%S UTC")


def _roles_text(user: UserRead) -> str:
    return ", ".join(user.roles) if user.roles else "(none)"


def _table(rows: list[list[str]], header: list[str]) -> str:
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header))]
    lines = [
        "  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip()
        for r in [header, *rows]
    ]
    return "\n".join(lines)


@user_app.command("add")
def add(
    username: Annotated[str, typer.Argument(help="The new user's name.")],
    roles: Annotated[
        str,
        typer.Option(
            "--roles",
            help="Comma-separated roles, e.g. hp-reader,hp-writer:demo (default: none).",
        ),
    ] = "",
    disabled: Annotated[bool, typer.Option("--disabled", help="Create the user disabled.")] = False,
    new_password_stdin: NewPasswordStdin = False,
    api_url: ApiUrl = DEFAULT_API_URL,
    token: Token = None,
    login_username: Username = None,
    password_stdin: PasswordStdin = False,
) -> None:
    """Create a user. Their password: hidden prompt (with confirmation), stdin or env."""
    with _session(api_url, token, login_username, password_stdin) as client:
        password = _new_password(new_password_stdin, password_stdin)
        user = client.create_user(
            username, password, roles=_split_roles(roles), is_active=not disabled
        )
    typer.echo(f"Created user {user.username!r} (roles: {_roles_text(user)})")


@user_app.command("list")
def list_(
    api_url: ApiUrl = DEFAULT_API_URL,
    token: Token = None,
    login_username: Username = None,
    password_stdin: PasswordStdin = False,
) -> None:
    """List every user."""
    with _session(api_url, token, login_username, password_stdin) as client:
        users = client.list_users()
    if not users:
        typer.echo("No users.")
        return
    rows = [
        [u.username, "yes" if u.is_active else "no", _roles_text(u), _when(u.last_login_at)]
        for u in users
    ]
    typer.echo(_table(rows, ["USERNAME", "ACTIVE", "ROLES", "LAST LOGIN"]))


@user_app.command("show")
def show(
    username: Name,
    api_url: ApiUrl = DEFAULT_API_URL,
    token: Token = None,
    login_username: Username = None,
    password_stdin: PasswordStdin = False,
) -> None:
    """Show one user."""
    with _session(api_url, token, login_username, password_stdin, what=username) as client:
        user = client.get_user(username)
    typer.echo(f"Username:    {user.username}")
    typer.echo(f"Active:      {'yes' if user.is_active else 'no'}")
    typer.echo(f"Roles:       {_roles_text(user)}")
    typer.echo(f"Created:     {_when(user.created_at)}")
    typer.echo(f"Updated:     {_when(user.updated_at)}")
    typer.echo(f"Last login:  {_when(user.last_login_at)}")


@user_app.command("set-password")
def set_password(
    username: Name,
    new_password_stdin: NewPasswordStdin = False,
    api_url: ApiUrl = DEFAULT_API_URL,
    token: Token = None,
    login_username: Username = None,
    password_stdin: PasswordStdin = False,
) -> None:
    """Set a user's password (their existing tokens stop working).

    The new password comes from a hidden prompt with confirmation, from
    --new-password-stdin or from env HELIOPUDDLE_NEW_PASSWORD; never from an option.
    """
    with _session(api_url, token, login_username, password_stdin, what=username) as client:
        password = _new_password(new_password_stdin, password_stdin)
        user = client.update_user(username, password=password)
    typer.echo(f"Password changed for {user.username!r}")


@user_app.command("set-roles")
def set_roles(
    username: Name,
    roles: Annotated[
        str,
        typer.Option(
            "--roles",
            help="Comma-separated roles replacing the current ones; '' removes them all.",
        ),
    ],
    api_url: ApiUrl = DEFAULT_API_URL,
    token: Token = None,
    login_username: Username = None,
    password_stdin: PasswordStdin = False,
) -> None:
    """Replace a user's roles (hp-admin or project roles).

    Project roles: hp-reader, hp-writer, hp-deleter, hp-operator and
    hp-client-storage-read-write, optionally scoped as :project.
    """
    with _session(api_url, token, login_username, password_stdin, what=username) as client:
        user = client.update_user(username, roles=_split_roles(roles))
    typer.echo(f"Roles of {user.username!r}: {_roles_text(user)}")


def _set_active(
    username: str,
    active: bool,
    api_url: str,
    token: str | None,
    login_username: str | None,
    password_stdin: bool,
) -> None:
    with _session(api_url, token, login_username, password_stdin, what=username) as client:
        user = client.update_user(username, is_active=active)
    typer.echo(f"User {user.username!r} {'enabled' if user.is_active else 'disabled'}")


@user_app.command("disable")
def disable(
    username: Name,
    api_url: ApiUrl = DEFAULT_API_URL,
    token: Token = None,
    login_username: Username = None,
    password_stdin: PasswordStdin = False,
) -> None:
    """Disable a user: they cannot log in and their tokens stop working."""
    _set_active(username, False, api_url, token, login_username, password_stdin)


@user_app.command("enable")
def enable(
    username: Name,
    api_url: ApiUrl = DEFAULT_API_URL,
    token: Token = None,
    login_username: Username = None,
    password_stdin: PasswordStdin = False,
) -> None:
    """Enable a disabled user."""
    _set_active(username, True, api_url, token, login_username, password_stdin)


@user_app.command("delete")
def delete(
    username: Name,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")] = False,
    api_url: ApiUrl = DEFAULT_API_URL,
    token: Token = None,
    login_username: Username = None,
    password_stdin: PasswordStdin = False,
) -> None:
    """Delete a user. The last active hp-admin cannot be deleted."""
    if not yes and not typer.confirm(f"Delete user {username!r}?"):
        _fail("not deleted", 1)
    with _session(api_url, token, login_username, password_stdin, what=username) as client:
        client.delete_user(username)
    typer.echo(f"Deleted user {username!r}")


__all__ = ["user_app"]

"""Command line: ``heliopuddle server start``, ``db upgrade``, ``user ...`` and ``file ...``.

See docs/design/deployment.md. Exit codes: 0 on success, 1 on a runtime
failure (migration, Prefect, a component stopping), 2 on invalid settings,
project config or options.
"""

from __future__ import annotations

import logging
from typing import Annotated

import typer

from heliopuddle.cli_file import file_app
from heliopuddle.cli_user import user_app

app = typer.Typer(no_args_is_help=True, help="heliopuddle: a multi-location datastore manager")
server_app = typer.Typer(no_args_is_help=True, help="Run heliopuddle processes")
db_app = typer.Typer(no_args_is_help=True, help="Database commands")
app.add_typer(server_app, name="server")
app.add_typer(db_app, name="db")
app.add_typer(user_app, name="user")
app.add_typer(file_app, name="file")

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def _configure_logging() -> None:
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    logging.getLogger("heliopuddle").setLevel(logging.INFO)


def _fail(message: str, code: int) -> typer.Exit:
    typer.echo(f"Error: {message}", err=True)
    return typer.Exit(code)


@server_app.command("start")
def start(
    host: Annotated[
        str | None, typer.Option(help="Interface to bind (overrides HELIOPUDDLE_HOST)")
    ] = None,
    port: Annotated[
        int | None, typer.Option(help="Port to bind (overrides HELIOPUDDLE_PORT)")
    ] = None,
    api: Annotated[bool, typer.Option("--api/--no-api", help="Serve the API at /api/v1")] = True,
    ui: Annotated[bool, typer.Option("--ui/--no-ui", help="Serve the UI")] = True,
    services: Annotated[
        bool, typer.Option("--services/--no-services", help="Serve the Prefect deployments")
    ] = True,
    migrate: Annotated[
        bool,
        typer.Option(
            "--migrate/--no-migrate",
            help="Run DB migrations first; with --no-migrate the schema must be at head",
        ),
    ] = True,
) -> None:
    """Start the API, UI and services (see docs/design/deployment.md)."""
    from heliopuddle.server.start import StartOptions, StartupError, run

    _configure_logging()
    options = StartOptions(host=host, port=port, api=api, ui=ui, services=services, migrate=migrate)
    try:
        run(options)
    except StartupError as e:
        raise _fail(str(e), e.exit_code) from None


@db_app.command("upgrade")
def upgrade() -> None:
    """Run database migrations only, then exit."""
    from heliopuddle.server.database.migrate import current_revision, upgrade_to_head
    from heliopuddle.server.start import StartupError, describe_db_url, load_settings

    _configure_logging()
    try:
        settings = load_settings()
    except StartupError as e:
        raise _fail(str(e), e.exit_code) from None
    url = settings.resolved_database_url
    db = describe_db_url(url)
    try:
        upgrade_to_head(url)
        revision = current_revision(url)
    except Exception as e:
        raise _fail(f"Database migration failed for {db}: {type(e).__name__}: {e}", 1) from None
    typer.echo(f"Database {db} is at revision {revision}")

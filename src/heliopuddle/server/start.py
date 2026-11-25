"""``heliopuddle server start``: run the API, UI and services in one process (DEP-02).

Startup order: settings → project config (the generated default config file if
there are no project files) and database (migrate or check) → config sync →
storage location roots checked or created → Prefect (external check or embedded server) →
Prefect block resolver installed and the roots of storage locations that use
blocks checked (an incomplete one is skipped with a warning) → index
concurrency limits and stale index deployments removed → served deployments →
adoption runs for storage locations never fully indexed → uvicorn. Shutdown
(SIGINT/SIGTERM) runs in reverse: uvicorn, the served deployments, then the
embedded Prefect server.

A UI-only process (``--no-api --no-services``) needs only settings: it does
not touch the database, project config or Prefect.
"""

from __future__ import annotations

import ipaddress
import logging
import os
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from sqlalchemy import make_url

from heliopuddle.config.loader import ConfigError, default_config_path, has_project_files
from heliopuddle.server.app import create_app, normalize_root_path, wrap_root_path
from heliopuddle.server.auth.secret import SecretKeyError
from heliopuddle.server.auth.startup import AuthStartupError, init_auth
from heliopuddle.server.context import AppContext, set_context
from heliopuddle.server.database.engine import refresh_statistics
from heliopuddle.server.database.migrate import (
    current_revision,
    head_revision,
    is_at_head,
    upgrade_to_head,
)
from heliopuddle.server.services.blocks import install_block_resolver
from heliopuddle.server.services.deploy import (
    ServicesRunner,
    build_deployments,
    prepare_index_services,
    prepare_reconcile_services,
    prepare_rule_services,
    reconcile_rule_services,
    rule_enablement,
    storage_location_ids,
    submit_adoption_runs,
)
from heliopuddle.server.services.runtime import (
    PrefectRuntime,
    PrefectStartupError,
    start_prefect,
)
from heliopuddle.server.serving import (
    EXIT_CONFIG,
    ServerSpec,
    Shutdown,
    StartupError,
    run_servers,
    wait_for_shutdown,
)
from heliopuddle.settings import Settings
from heliopuddle.storage import StorageLocation, StorageRootMissingError
from heliopuddle.storage.credentials import StorageLocationIncomplete, open_location
from heliopuddle.storage.redact import redact_text, redact_url

logger = logging.getLogger(__name__)


@dataclass
class StartOptions:
    """Command-line options of ``server start``; None means "use the settings"."""

    host: str | None = None
    port: int | None = None
    api: bool = True
    ui: bool = True
    services: bool = True
    migrate: bool = True


def describe_db_url(url: str) -> str:
    """Return ``url`` with any password hidden, for messages."""
    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:
        return "<invalid database URL>"


def format_settings_error(error: ValidationError) -> str:
    """Name each invalid setting by its environment variable."""
    lines = ["Invalid settings:"]
    for err in error.errors():
        name = ".".join(str(p) for p in err["loc"])
        env = f"HELIOPUDDLE_{name.upper()}" if name else "(settings)"
        lines.append(f"  {env}: {err['msg']}")
    return "\n".join(lines)


def load_settings(host: str | None = None, port: int | None = None) -> Settings:
    """Read settings from the environment; command-line values win.

    Raises:
        StartupError: (exit code 2) naming each invalid setting.
    """
    overrides: dict[str, Any] = {}
    if host is not None:
        overrides["host"] = host
    if port is not None:
        overrides["port"] = port
    try:
        return Settings(**overrides)
    except ValidationError as e:
        raise StartupError(format_settings_error(e), EXIT_CONFIG) from None


def ensure_storage_roots(ctx: AppContext, *, blocks: bool = False) -> None:
    """Check, and create where ``create_root`` allows, every storage location root (STO-08).

    Every storage location in the project config is active, so each one is
    checked. Storage locations that use Prefect blocks (STO-18, STO-19) are
    checked in a second pass (``blocks=True``) once Prefect is up; one that is
    incomplete (a block is missing or Prefect cannot be reached) is skipped
    with a warning naming the problems, never a startup error.

    Args:
        ctx: the app context.
        blocks: check the storage locations that use Prefect blocks (True) or
            the others (False).

    Raises:
        StartupError: (exit code 2) naming the project, storage location and
            URL of the first root that is missing and cannot be created.
    """
    for project in ctx.projects.values():
        for cfg in project.storage_locations:
            if cfg.uses_blocks != blocks:
                continue
            try:
                open_location(cfg).ensure_root()
            except StorageLocationIncomplete as e:
                logger.warning(
                    "Storage location %r of project %r is incomplete and is not checked; "
                    "it cannot be used until this is fixed (no restart needed): %s",
                    cfg.name,
                    project.project,
                    "; ".join(e.problems),
                )
            except Exception as e:
                detail = redact_text(
                    str(e) if isinstance(e, StorageRootMissingError) else f"{type(e).__name__}: {e}"
                )
                raise StartupError(
                    f"Cannot use storage location {cfg.name!r} of project {project.project!r} "
                    f"({redact_url(cfg.display_url)}): {detail}",
                    EXIT_CONFIG,
                ) from None


def prepare_database(settings: Settings, migrate: bool = True) -> AppContext:
    """Load project config, migrate (or check) the database, sync config and check roots.

    Args:
        settings: process settings.
        migrate: run pending migrations; when False, the schema must already
            be at head.

    Returns:
        The app context, installed as the process context.

    Raises:
        StartupError: on invalid config or a storage location root that is
            missing and cannot be created (exit code 2), a failed migration, a
            schema not at head with ``migrate=False``, or a failed config sync.
    """
    try:
        ctx = AppContext.from_settings(settings)
    except ConfigError as e:
        raise StartupError(f"Invalid project config:\n{e}", EXIT_CONFIG) from None
    if not has_project_files(settings.config_dir):
        store = ctx.projects[next(iter(ctx.projects))].storage_location()
        logger.info(
            "No project config found in %s; using the default config file %s, a 'default' "
            "project storing files in %s; edit it, or set HELIOPUDDLE_CONFIG_DIR to a "
            "directory of project YAML files to configure your own",
            settings.config_dir.resolve(),
            default_config_path(settings.data_dir).resolve(),
            StorageLocation(store.display_url).root,
        )

    db = describe_db_url(settings.resolved_database_url)
    try:
        if migrate:
            logger.info("Migrating the database %s", db)
            try:
                upgrade_to_head(ctx.engine)
            except Exception as e:
                raise StartupError(
                    f"Database migration failed for {db}: {type(e).__name__}: {e}"
                ) from e
        else:
            try:
                at_head = is_at_head(ctx.engine)
                current = current_revision(ctx.engine)
            except Exception as e:
                raise StartupError(
                    f"Cannot read the schema revision of {db}: {type(e).__name__}: {e}"
                ) from e
            if not at_head:
                raise StartupError(
                    f"Database {db} is at schema revision {current or '(none)'}, not at head "
                    f"{head_revision()}. Run `heliopuddle db upgrade`, or start without "
                    "--no-migrate."
                )
        try:
            ctx.sync_config()
        except Exception as e:
            raise StartupError(
                f"Syncing project config into {db} failed: {type(e).__name__}: {e}"
            ) from e
        ensure_storage_roots(ctx)
        try:
            init_auth(ctx)
        except AuthStartupError as e:
            raise StartupError(str(e), EXIT_CONFIG) from None
        if ctx.engine.dialect.name == "sqlite":
            dbapi = ctx.engine.dialect.dbapi
            logger.info(
                "Using SQLite %s (%s)",
                getattr(dbapi, "sqlite_version", "?"),
                getattr(dbapi, "__name__", "?"),
            )
        refresh_statistics(ctx.engine)
    except StartupError:
        ctx.dispose()
        raise
    set_context(ctx)
    return ctx


def export_settings_env(settings: Settings) -> None:
    """Put absolute paths of the settings into the environment for child processes.

    Flow runs of served deployments run in child processes and rebuild the
    context from ``HELIOPUDDLE_*`` variables, so they must see the same config
    directory, data directory and database as this process.
    """
    os.environ["HELIOPUDDLE_CONFIG_DIR"] = str(settings.config_dir.resolve())
    os.environ["HELIOPUDDLE_DATA_DIR"] = str(settings.data_dir.resolve())
    os.environ["HELIOPUDDLE_DATABASE_URL"] = settings.resolved_database_url


def check_options(settings: Settings, options: StartOptions) -> None:
    """Reject option combinations that cannot work.

    Raises:
        StartupError: (exit code 2) when nothing is enabled, the UI runs
            without the API but ``HELIOPUDDLE_UI_API_URL`` is unset, or a
            shared port has two different root paths.
    """
    if not (options.api or options.ui or options.services):
        raise StartupError("Nothing to run: --no-api, --no-ui and --no-services", EXIT_CONFIG)
    if options.ui and not options.api and not settings.ui_api_url:
        raise StartupError(
            "The UI without the API (--no-api) needs HELIOPUDDLE_UI_API_URL, "
            "the URL of an API (ending in /api/v1)",
            EXIT_CONFIG,
        )
    if options.api and options.ui and settings.ui_shares_api_port:
        api_rp = normalize_root_path(settings.api_root_path)
        ui_rp = normalize_root_path(settings.ui_root_path)
        if api_rp and ui_rp and api_rp != ui_rp:
            raise StartupError(
                f"The API and UI share port {settings.port} but have different root paths "
                f"(HELIOPUDDLE_API_ROOT_PATH={api_rp}, HELIOPUDDLE_UI_ROOT_PATH={ui_rp}); "
                "make them equal or give the UI its own HELIOPUDDLE_UI_PORT",
                EXIT_CONFIG,
            )


def is_loopback_host(host: str) -> bool:
    """Whether a bind host is only reachable from this machine."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def warn_if_header_mode_exposed(settings: Settings) -> None:
    """Log a WARNING when ``header`` mode listens beyond loopback (AUTH-20).

    In ``header`` mode anyone who can reach the port can send the user header
    themselves, so only a trusted proxy may be able to.
    """
    if settings.auth_mode != "header":
        return
    for host in {settings.host, settings.ui_host or settings.host}:
        if not is_loopback_host(host):
            logger.warning(
                "HELIOPUDDLE_AUTH_MODE=header trusts the %s header from every client that can "
                "reach %s: bind to 127.0.0.1 behind your authenticating proxy, or use local or "
                "oidc mode",
                settings.user_header,
                host,
            )


def plan_servers(
    settings: Settings, ctx: AppContext | None, options: StartOptions
) -> list[ServerSpec]:
    """Decide which uvicorn servers to run and build their apps (UI-03).

    - API and UI on one port (default): one server, API at ``/api/v1``, UI at ``/``.
    - ``HELIOPUDDLE_UI_PORT`` differs from the port: a second server for the UI.
    - UI without the API: one UI server on ``UI_HOST``/``UI_PORT`` (default
      ``HOST``/``PORT``).

    Args:
        settings: process settings.
        ctx: the app context (needed when the API runs).
        options: which parts run.

    Raises:
        StartupError: (exit code 2) the UI needs the auth secret for its session
            cookie (any ``auth_mode`` but ``header``) and there is none.
    """
    warn_if_header_mode_exposed(settings)
    try:
        return _plan_servers(settings, ctx, options)
    except SecretKeyError as e:  # the UI's session cookie needs the auth secret (AUTH-06)
        raise StartupError(str(e), EXIT_CONFIG) from None


def _plan_servers(
    settings: Settings, ctx: AppContext | None, options: StartOptions
) -> list[ServerSpec]:
    ui_host = settings.ui_host or settings.host
    ui_port = settings.ui_port or settings.port
    api_rp = normalize_root_path(settings.api_root_path)
    ui_rp = normalize_root_path(settings.ui_root_path)
    servers: list[ServerSpec] = []
    if options.api and options.ui and settings.ui_shares_api_port:
        app = create_app(settings, ctx, api=True, ui=True)
        servers.append(
            ServerSpec("api+ui", settings.host, settings.port, wrap_root_path(app, api_rp or ui_rp))
        )
        return servers
    if options.api:
        app = create_app(settings, ctx, api=True, ui=False)
        servers.append(ServerSpec("api", settings.host, settings.port, wrap_root_path(app, api_rp)))
    if options.ui:
        app = create_app(settings, None, api=False, ui=True)
        servers.append(ServerSpec("ui", ui_host, ui_port, wrap_root_path(app, ui_rp)))
    return servers


def run(options: StartOptions) -> None:
    """Start everything ``options`` enables and block until shutdown.

    Args:
        options: command-line options.

    Raises:
        StartupError: if startup fails or a component stops unexpectedly.
    """
    settings = load_settings(options.host, options.port)
    check_options(settings, options)

    ctx: AppContext | None = None
    prefect: PrefectRuntime | None = None
    services: ServicesRunner | None = None
    shutdown = Shutdown()
    shutdown.install()
    try:
        if options.api or options.services:
            ctx = prepare_database(settings, migrate=options.migrate)
            export_settings_env(settings)
            try:
                prefect = start_prefect(settings)
            except PrefectStartupError as e:
                raise StartupError(str(e)) from None
            # Blocks resolve lazily through Prefect, so only now (STO-18, STO-19).
            install_block_resolver()
            assert ctx is not None
            ensure_storage_roots(ctx, blocks=True)
        if shutdown.event.is_set():
            return
        if options.services:
            assert ctx is not None
            try:
                deployments = build_deployments(
                    projects=ctx.projects,
                    ids=storage_location_ids(ctx),
                    rule_enabled=rule_enablement(ctx),
                )
                prepare_index_services(ctx.projects, deployments)
                prepare_rule_services(ctx.projects, deployments)
                prepare_reconcile_services(deployments)
            except Exception as e:
                raise StartupError(
                    f"Preparing the service deployments failed: {type(e).__name__}: {e}"
                ) from None
            services = ServicesRunner(deployments)
            try:
                services.start()
                reconcile_rule_services(ctx)
            except Exception as e:
                raise StartupError(str(e)) from None
            submit_adoption_runs(ctx, deployments)
        specs = plan_servers(settings, ctx, options) if (options.api or options.ui) else []
        if specs:
            run_servers(specs, shutdown, services)
        else:
            wait_for_shutdown(shutdown, services)
    finally:
        if services is not None:
            services.stop()
        if prefect is not None:
            prefect.stop()
        if ctx is not None:
            set_context(None)
            ctx.dispose()
        shutdown.restore()
        logger.info("Stopped")

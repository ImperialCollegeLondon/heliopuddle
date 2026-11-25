"""Process settings, read from ``HELIOPUDDLE_*`` environment variables."""

from __future__ import annotations

import os
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import ByteSize, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from heliopuddle.config.models import Size
from heliopuddle.durations import Duration

MIN_PASSWORD_LENGTH = 12
"""Minimum length of a local user's password (AUTH-03, AUTH-13)."""

MIN_SECRET_KEY_LENGTH = 32
"""Minimum length of ``HELIOPUDDLE_AUTH_SECRET_KEY`` (HS256 wants at least 32 bytes; AUTH-06)."""

USERNAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._@+-]*$"
"""Local usernames: ASCII letters, digits and ``. _ @ + -``, starting with a letter or digit.

ASCII keeps case-insensitive matching identical in SQL and Python (SQLite's ``lower()``
is ASCII-only), and the characters are safe in one URL path segment (``/users/{name}``).
"""


class Settings(BaseSettings):
    """Settings for every heliopuddle process.

    Project configuration (storage locations, rules, ...) lives in YAML files
    under ``config_dir``; see :mod:`heliopuddle.config`.
    """

    model_config = SettingsConfigDict(env_prefix="HELIOPUDDLE_", extra="ignore")

    config_dir: Path = Path("./config")
    data_dir: Path = Path("./.heliopuddle")
    database_url: str | None = Field(
        default=None, description="SQLAlchemy URL; defaults to SQLite under data_dir"
    )

    host: str = "127.0.0.1"
    port: int = 8080
    api_root_path: str = ""

    user_header: str = Field(
        default="X-Forwarded-User",
        description="Header carrying the user id; trusted only when auth_mode is 'header'",
    )

    auth_mode: Literal["local", "oidc", "header"] = Field(
        default="local",
        description="'local': users and passwords in the DB; 'oidc': tokens from an external "
        "provider such as Keycloak; 'header': a trusted proxy sets user_header (AUTH-01)",
    )
    admin_username: str = Field(
        default="admin",
        min_length=1,
        max_length=255,
        pattern=USERNAME_PATTERN,
        description="First admin (AUTH-03)",
    )
    admin_password: SecretStr | None = Field(
        default=None, description="First admin's password; generated and logged once when unset"
    )
    admin_reset: bool = Field(
        default=False, description="Reset the admin's password at startup (needs admin_password)"
    )
    auth_secret_key: SecretStr | None = Field(
        default=None, description="Signs local tokens and UI sessions; generated into data_dir"
    )
    auth_token_ttl: Duration = Field(
        default=timedelta(hours=8), description="Lifetime of a local access token"
    )

    login_max_failures: int = Field(
        default=10,
        ge=1,
        description="Failed password logins per username before it is blocked for "
        "login_window (AUTH-18)",
    )
    login_max_failures_per_ip: int = Field(
        default=100,
        ge=1,
        description="Failed password logins per client address before it is blocked for "
        "login_window; higher than the per-username limit because the UI's API calls share "
        "one address (AUTH-18)",
    )
    login_window: Duration = Field(
        default=timedelta(minutes=15),
        description="How long failed logins are counted, and how long a block lasts",
    )

    oidc_issuer: str | None = Field(default=None, description="e.g. https://host/realms/<realm>")
    oidc_discovery_url: str | None = Field(
        default=None,
        description="Where hp reads the provider's discovery document, when hp reaches the "
        "provider at another address than browsers do (e.g. on a private network); default: "
        "<issuer>/.well-known/openid-configuration (AUTH-21)",
    )
    oidc_client_id: str | None = None
    oidc_client_secret: SecretStr | None = None
    oidc_audience: str | None = Field(default=None, description="Defaults to the client id")
    oidc_username_claim: str = "preferred_username"
    oidc_roles_claim: str = Field(
        default="realm_access.roles", description="Dotted path to the roles list in the token"
    )
    oidc_introspect: Literal["off", "sensitive", "always"] = Field(
        default="sensitive",
        description="Ask the provider whether a token is still active: never, on routes "
        "needing hp-deleter, hp-client-storage-read-write or hp-admin, or on every route",
    )

    ui_host: str | None = Field(default=None, description="Defaults to host")
    ui_port: int | None = Field(
        default=None, description="Unset or equal to port: the UI shares the API's port"
    )
    ui_api_url: str | None = Field(
        default=None, description="API base URL the UI calls; defaults to this process's API"
    )
    ui_root_path: str = ""

    prefect_ui_url: str | None = Field(
        default=None, description="Link target for the Prefect UI; derived from PREFECT_API_URL"
    )

    frame_ancestors: str = Field(
        default="'none'",
        description="Who may show hp pages in a frame: the sources of the CSP frame-ancestors "
        "directive, e.g. 'self' for a portal on the same origin (AUTH-19)",
    )

    op_lease: Duration = Field(
        default=timedelta(hours=1),
        description="How long an upload, overwrite or delete owns a path before it "
        "expires and may be repaired (renew extends it)",
    )

    preview_max_bytes: Size = Field(
        default=ByteSize(1_000_000_000),
        gt=0,
        description="The largest CDF copied to a temporary file to preview it from a "
        "storage location without local file paths (API-18)",
    )

    max_upload_size: Size = Field(
        default=ByteSize(1 * 1024**3),
        gt=0,
        description="The largest file a reservation or registration may declare, e.g. "
        "'1GiB'; bigger ones get 413 (API-23)",
    )

    rclone_binary: str | None = Field(
        default=None,
        description="The rclone executable for rclone storage locations; default: rclone "
        "on PATH, else the pinned release downloaded into <data dir>/bin (DEP-21)",
    )
    rclone_auto_download: bool = Field(
        default=True,
        description="Download the pinned rclone release when no rclone is found (DEP-21)",
    )
    rclone_api_wait: Duration = Field(
        default=timedelta(seconds=30),
        description="How long an API request waits for a busy rclone config before "
        "answering 503 with Retry-After (STO-20)",
    )
    rclone_idle: Duration = Field(
        default=timedelta(seconds=2),
        description="How long an rclone session (and its rcd and config lock) stays "
        "open after its last use (STO-20)",
    )

    @field_validator("admin_password")
    @classmethod
    def _admin_password_length(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value()) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"must be at least {MIN_PASSWORD_LENGTH} characters")
        return value

    @field_validator("auth_secret_key")
    @classmethod
    def _secret_key_strength(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value()) < MIN_SECRET_KEY_LENGTH:
            raise ValueError(
                f"must be at least {MIN_SECRET_KEY_LENGTH} characters (for example the output of "
                "`openssl rand -hex 32`); an empty or short value would make tokens and "
                "sessions forgeable. Unset it to let hp generate and keep a random key"
            )
        return value

    @field_validator("frame_ancestors")
    @classmethod
    def _one_frame_ancestors_directive(cls, value: str) -> str:
        sources = value.strip(" ").split(" ")
        if (
            not value.strip(" ")
            or not value.isprintable()
            or any(c in value for c in ";,")
            or any(s.lower() in ("none", "self") for s in sources)
            or ("'none'" in sources and len([s for s in sources if s]) > 1)
        ):
            raise ValueError(
                "must be 'none' or one or more frame-ancestors sources separated by spaces, "
                "such as 'self' (with the quotes) or https://portal.example.org"
            )
        return " ".join(s for s in sources if s)

    @field_validator("auth_token_ttl", "login_window")
    @classmethod
    def _ttl_positive(cls, value: timedelta) -> timedelta:
        if value <= timedelta():
            raise ValueError("must be greater than zero")
        return value

    @model_validator(mode="after")
    def _auth(self) -> Settings:
        if self.admin_reset and self.admin_password is None:
            raise ValueError("HELIOPUDDLE_ADMIN_RESET needs HELIOPUDDLE_ADMIN_PASSWORD")
        if self.auth_mode == "oidc":
            if not self.oidc_issuer:
                raise ValueError("HELIOPUDDLE_OIDC_ISSUER is required when auth_mode is oidc")
            if not self.oidc_client_id:
                raise ValueError("HELIOPUDDLE_OIDC_CLIENT_ID is required when auth_mode is oidc")
            if self.oidc_introspect != "off" and self.oidc_client_secret is None:
                raise ValueError(
                    "HELIOPUDDLE_OIDC_CLIENT_SECRET is required when auth_mode is oidc and "
                    "HELIOPUDDLE_OIDC_INTROSPECT is not off"
                )
        return self

    @model_validator(mode="after")
    def _defaults(self) -> Settings:
        if self.database_url is None:
            self.database_url = f"sqlite:///{(self.data_dir / 'heliopuddle.db').resolve()}"
        if self.ui_host is None:
            self.ui_host = self.host
        return self

    @property
    def ui_shares_api_port(self) -> bool:
        """True when the UI is served by the same uvicorn server as the API."""
        return self.ui_port is None or self.ui_port == self.port

    @property
    def resolved_oidc_issuer(self) -> str:
        """The OIDC issuer without a trailing slash (``""`` when unset)."""
        return (self.oidc_issuer or "").rstrip("/")

    @property
    def resolved_oidc_audience(self) -> str:
        """The expected token audience: ``oidc_audience``, else the client id."""
        return self.oidc_audience or self.oidc_client_id or ""

    @property
    def resolved_database_url(self) -> str:
        """The database URL (never None after validation)."""
        assert self.database_url is not None
        return self.database_url


@lru_cache
def get_settings() -> Settings:
    """Return the process settings (cached; call ``get_settings.cache_clear()`` in tests)."""
    return Settings()


def prefect_ui_url(settings: Settings) -> str | None:
    """The Prefect UI base URL, or None when it is not known.

    Used for links in the UI and for ``flow_run_url`` in run responses.

    Args:
        settings: process settings.

    Returns:
        ``settings.prefect_ui_url`` if set; otherwise ``PREFECT_API_URL`` from
        the environment with a trailing ``/api`` removed. No trailing slash.
        Read on each call, because the embedded Prefect server sets the
        variable at startup.
    """
    url = settings.prefect_ui_url or os.environ.get("PREFECT_API_URL") or ""
    url = url.rstrip("/")
    if not settings.prefect_ui_url and url.endswith("/api"):
        url = url[: -len("/api")]
    return url or None

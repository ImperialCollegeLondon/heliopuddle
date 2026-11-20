"""Print the ``HELIOPUDDLE_*`` environment variable tables for the user docs.

The server table is generated from :class:`heliopuddle.settings.Settings`; the
client and CLI variables are read directly by the CLI options and are listed
here. ``docs/site/environment.md`` embeds the output between the
``env-table`` markers; ``tests/unit/test_env_table.py`` fails when they differ.

Usage: ``uv run python scripts/env_table.py`` (print) or ``... --write`` (update the page).
"""

from __future__ import annotations

import re
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

from pydantic import ByteSize, SecretStr
from pydantic_core import PydanticUndefined

from heliopuddle.settings import Settings

# Descriptions for settings whose model field has none.
_DESCRIPTIONS = {
    "config_dir": "Directory of project YAML files. Docker image: `/config`.",
    "data_dir": "State directory (SQLite database, signing key, staging). "
    "Docker image: `/var/lib/heliopuddle`.",
    "host": "Interface the server binds (`--host` overrides).",
    "port": "Port the server binds (`--port` overrides).",
    "api_root_path": "URL prefix when the API is served behind a reverse proxy under a sub-path.",
    "oidc_client_id": "OIDC client id (required when `auth_mode` is `oidc`).",
    "oidc_client_secret": "OIDC client secret (required in `oidc` mode unless "
    "`oidc_introspect` is `off`).",
    "oidc_username_claim": "Token claim holding the user name.",
    "ui_root_path": "URL prefix when the UI is served behind a reverse proxy under a sub-path.",
}

# Read by the client and CLI (not part of Settings): (name, default, description).
CLIENT_VARIABLES = [
    (
        "HELIOPUDDLE_API_URL",
        "",
        "API base URL for `heliopuddle file` and `user`, e.g. `http://localhost:8080/api/v1`.",
    ),
    ("HELIOPUDDLE_PROJECT", "`default`", "Project for `heliopuddle file`."),
    ("HELIOPUDDLE_USERNAME", "", "Account the CLI logs in as."),
    (
        "HELIOPUDDLE_PASSWORD",
        "",
        "Password for `HELIOPUDDLE_USERNAME` (else a hidden prompt or `--password-stdin`).",
    ),
    (
        "HELIOPUDDLE_NEW_PASSWORD",
        "",
        "New password for `heliopuddle user` commands that set one (else a prompt).",
    ),
    ("HELIOPUDDLE_TOKEN", "", "Bearer token for the CLI, instead of a username and password."),
    (
        "HELIOPUDDLE_USER",
        "",
        "User id sent in the user header, for servers in `header` auth mode.",
    ),
]


def _size_text(n: int) -> str:
    """Format a byte count exactly, in the largest binary or decimal unit that divides it."""
    for unit, factor in (
        ("TiB", 1024**4),
        ("TB", 1000**4),
        ("GiB", 1024**3),
        ("GB", 1000**3),
        ("MiB", 1024**2),
        ("MB", 1000**2),
        ("KiB", 1024),
        ("kB", 1000),
    ):
        if n >= factor and n % factor == 0:
            return f"{n // factor}{unit}"
    return f"{n}B"


def _default(info: Any) -> str:
    default = info.default
    if default is PydanticUndefined or default is None or default == "":
        return ""
    if isinstance(default, SecretStr):
        return ""
    if isinstance(default, timedelta):
        seconds = int(default.total_seconds())
        for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
            if seconds % size == 0:
                return f"`{seconds // size}{unit}`"
        return f"`{seconds}s`"
    if isinstance(default, ByteSize):
        return f"`{_size_text(int(default))}`"
    if isinstance(default, bool):
        return f"`{str(default).lower()}`"
    return f"`{default}`"


def _escape(text: str) -> str:
    text = re.sub(r" \([A-Z]+-\d+\)", "", " ".join(text.split()))
    return text.replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;")


def _table(rows: list[tuple[str, str, str]]) -> str:
    lines = ["| Variable | Default | Description |", "|---|---|---|"]
    lines += [f"| `{n}` | {d} | {_escape(t)} |" for n, d, t in rows]
    return "\n".join(lines)


def server_rows() -> list[tuple[str, str, str]]:
    """Rows (name, default, description) for every field of ``Settings``."""
    prefix = Settings.model_config.get("env_prefix", "")
    rows = []
    for name, info in Settings.model_fields.items():
        description = info.description or _DESCRIPTIONS.get(name, "")
        rows.append((f"{prefix}{name}".upper(), _default(info), description))
    return rows


def render() -> str:
    """The markdown for both tables."""
    return (
        "### Server settings\n\n"
        + _table(server_rows())
        + "\n\n### Client and CLI\n\n"
        + _table(CLIENT_VARIABLES)
    )


PAGE = Path(__file__).resolve().parent.parent / "docs" / "site" / "environment.md"
START = "<!-- env-table:start -->"
END = "<!-- env-table:end -->"


def embedded(page_text: str) -> str:
    """The text between the markers of the docs page, stripped."""
    return page_text.split(START, 1)[1].split(END, 1)[0].strip()


def update_page() -> None:
    """Rewrite the docs page's table block from the settings models."""
    text = PAGE.read_text(encoding="utf-8")
    head, rest = text.split(START, 1)
    tail = rest.split(END, 1)[1]
    PAGE.write_text(f"{head}{START}\n{render()}\n{END}{tail}", encoding="utf-8")


if __name__ == "__main__":
    if "--write" in sys.argv[1:]:
        update_page()
    else:
        print(render())

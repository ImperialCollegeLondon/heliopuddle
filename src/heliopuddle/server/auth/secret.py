"""The signing key for local tokens and UI sessions (AUTH-06)."""

from __future__ import annotations

import os
import secrets
import tempfile
import time
from pathlib import Path

from heliopuddle.settings import MIN_SECRET_KEY_LENGTH, Settings

SECRET_FILE = "auth_secret"
"""Name of the generated key file inside the data dir."""

KEY_BYTES = 32


class SecretKeyError(Exception):
    """No signing key is available; startup stops with this message."""


def _unavailable(detail: str) -> SecretKeyError:
    return SecretKeyError(
        f"no auth signing key: {detail}; set HELIOPUDDLE_AUTH_SECRET_KEY "
        "(the same value for every hp process that must accept the same tokens)"
    )


def _read(path: Path) -> bytes | None:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise _unavailable(f"cannot read {path} ({e.strerror or e})") from None
    return data or None


def _create_exclusive(path: Path, key: bytes) -> None:
    """Create ``path`` with ``O_EXCL`` (mode 0600); if another process owns it, wait for its key."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        for _ in range(50):  # the winner may not have written yet
            if _read(path) is not None:
                return
            time.sleep(0.1)
        raise _unavailable(f"{path} exists but is empty") from None
    except OSError as e:
        raise _unavailable(f"cannot create {path} ({e.strerror or e})") from None
    with os.fdopen(fd, "wb") as f:
        f.write(key)


def load_secret(settings: Settings) -> bytes:
    """Return the signing key, generating and persisting it on first use.

    ``HELIOPUDDLE_AUTH_SECRET_KEY`` wins. Otherwise the key is read from
    ``DATA_DIR/auth_secret``; if the file does not exist, 32 random bytes are
    written to it (mode 0600). The file is created atomically (written to a
    temporary file, then hard-linked into place), so concurrent first starts
    all end up using the same key.

    An env var that is set but empty or shorter than
    :data:`~heliopuddle.settings.MIN_SECRET_KEY_LENGTH` is an error, never a
    fallback to generating a key and never used as it is: an empty deployment
    variable must not silently select a publicly known signing key (it would
    also make the UI session key reproducible).

    Raises:
        SecretKeyError: the env var is empty or too short, or there is no env
            var and no readable or creatable key file in the data dir; the
            message names ``HELIOPUDDLE_AUTH_SECRET_KEY``.
    """
    if settings.auth_secret_key is not None:
        configured = settings.auth_secret_key.get_secret_value()
        if len(configured) < MIN_SECRET_KEY_LENGTH:
            raise SecretKeyError(
                f"HELIOPUDDLE_AUTH_SECRET_KEY must be at least {MIN_SECRET_KEY_LENGTH} "
                "characters (for example `openssl rand -hex 32`), or unset"
            )
        return configured.encode()
    path = settings.data_dir / SECRET_FILE
    existing = _read(path)
    if existing is not None:
        return existing
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".auth_secret.")  # mode 0600
    except OSError as e:
        raise _unavailable(f"cannot create {path} ({e.strerror or e})") from None
    key = secrets.token_bytes(KEY_BYTES)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(key)
        try:
            os.link(tmp, path)  # fails if a concurrent creator won
        except FileExistsError:
            pass
        except OSError:
            _create_exclusive(path, key)  # no hard links on this filesystem
    finally:
        Path(tmp).unlink(missing_ok=True)
    stored = _read(path)
    if stored is None:
        raise _unavailable(f"cannot read {path} after creating it")
    return stored


_cache: dict[tuple[str | None, Path], bytes] = {}


def get_secret(settings: Settings) -> bytes:
    """Return the key of :func:`load_secret`, remembered per process (no file read per request)."""
    env = None if settings.auth_secret_key is None else settings.auth_secret_key.get_secret_value()
    cache_key = (env, settings.data_dir)
    key = _cache.get(cache_key)
    if key is None:
        key = _cache[cache_key] = load_secret(settings)
    return key

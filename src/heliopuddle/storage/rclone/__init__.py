"""The ``rclone://`` storage backend: fsspec over a private local ``rclone rcd``.

A backend package like s3fs: with ``backends.py`` it is the only code that
knows about rclone. See docs/design/storage.md ("rclone").

The server wires it up with :func:`configure` (where the binary comes from,
the session idle time) and :func:`set_session_hooks` (the cluster-wide config
lock and the config block store); API requests bound their wait for a config
lock with :func:`wait_limit` and answer ``503`` on :class:`RcloneBusyError`.
"""

from __future__ import annotations

from pathlib import Path

from heliopuddle.storage.rclone.binary import RCLONE_VERSION, configure_binary, find_rclone
from heliopuddle.storage.rclone.config import BlockStore, ConfigSource, source_from_options
from heliopuddle.storage.rclone.errors import (
    RcloneBusyError,
    RcloneConfigError,
    RcloneError,
    RcloneUnavailable,
    RcloneVerifyError,
)
from heliopuddle.storage.rclone.session import (
    LockFactory,
    SessionManager,
    manager,
    set_session_hooks,
    wait_limit,
)

__all__ = [
    "RCLONE_VERSION",
    "BlockStore",
    "ConfigSource",
    "LockFactory",
    "RcloneBusyError",
    "RcloneConfigError",
    "RcloneError",
    "RcloneUnavailable",
    "RcloneVerifyError",
    "SessionManager",
    "configure",
    "find_rclone",
    "manager",
    "set_session_hooks",
    "source_from_options",
    "wait_limit",
]

_UNCHANGED: object = object()


def configure(
    *,
    binary: str | object | None = _UNCHANGED,
    download_dir: str | Path | object | None = _UNCHANGED,
    auto_download: bool | object = _UNCHANGED,
    idle: float | object = _UNCHANGED,
    tmp_dir: str | Path | object | None = _UNCHANGED,
) -> None:
    """Set where rclone comes from and how long sessions stay open; omitted values are kept.

    Args:
        binary: an explicit rclone executable (``HELIOPUDDLE_RCLONE_BINARY``).
        download_dir: where the pinned release is downloaded (``<data dir>/bin``).
        auto_download: download it when no rclone is found
            (``HELIOPUDDLE_RCLONE_AUTO_DOWNLOAD``).
        idle: seconds a session stays open after its last use (``HELIOPUDDLE_RCLONE_IDLE``).
        tmp_dir: where session and spool directories are made (default: system temp).
    """
    changes: dict[str, object] = {}
    if binary is not _UNCHANGED:
        changes["binary"] = binary
    if download_dir is not _UNCHANGED:
        changes["download_dir"] = Path(str(download_dir)) if download_dir is not None else None
    if auto_download is not _UNCHANGED:
        changes["auto_download"] = bool(auto_download)
    if changes:
        configure_binary(**changes)
    if idle is not _UNCHANGED:
        manager.idle = float(idle)  # type: ignore[arg-type]
    if tmp_dir is not _UNCHANGED:
        manager.tmp_dir = str(tmp_dir) if tmp_dir is not None else None

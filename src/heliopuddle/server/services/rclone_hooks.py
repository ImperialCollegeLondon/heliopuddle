"""Server wiring of the ``rclone://`` backend (STO-05, STO-20, DEP-21).

:func:`install_rclone` gives :mod:`heliopuddle.storage.rclone` what it cannot
know itself:

- where the binary comes from (``HELIOPUDDLE_RCLONE_*`` settings);
- the cluster-wide config lock: one slot of the Prefect global concurrency
  limit ``rclone:<config>`` per config, so only one rcd uses a config at a
  time across every process and node;
- the config block store: prefect-managedfiletransfer's
  ``RCloneConfigFileBlock`` documents, read at session start and written back
  at session end;
- a problem check that marks an rclone storage location incomplete (STO-18)
  when rclone, its config block or its remote is missing.

See docs/design/storage.md ("rclone").
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from heliopuddle.server.services.concurrency import ensure_concurrency_limit, hp_concurrency
from heliopuddle.settings import Settings
from heliopuddle.storage import backends, rclone
from heliopuddle.storage.credentials import register_problem_check, unregister_problem_check
from heliopuddle.storage.rclone.config import load_config, parse_sections

logger = logging.getLogger(__name__)

__all__ = [
    "PROBLEM_CACHE_TTL",
    "PrefectConfigBlockStore",
    "close_rclone_sessions",
    "config_block_class",
    "install_rclone",
    "prefect_lock",
    "rclone_problems",
    "register_rclone_block_type",
]

PROBLEM_CACHE_TTL = 60.0
"""Seconds a config block lookup made by the problem check is reused."""

_installed = False
_install_lock = threading.Lock()


def config_block_class() -> type[Any]:
    """The ``RCloneConfigFileBlock`` block class.

    prefect-managedfiletransfer's class when that package is installed;
    otherwise an identical one (same block type name and fields), so blocks
    made by either work with both.
    """
    try:
        from prefect_managedfiletransfer import (  # pyright: ignore[reportMissingImports]
            RCloneConfigFileBlock,
        )

        return RCloneConfigFileBlock  # pyright: ignore[reportUnknownVariableType]
    except ImportError:
        return _own_block_class()


_OWN_BLOCK: type[Any] | None = None


def _own_block_class() -> type[Any]:
    global _OWN_BLOCK
    if _OWN_BLOCK is None:
        from prefect.blocks.core import Block
        from pydantic import Field

        class RCloneConfigFileBlock(Block):
            """An rclone config file, compatible with prefect-managedfiletransfer's block.

            Refreshed OAuth tokens are written back to ``config_file_contents``.
            """

            _block_type_name = "RClone Remote Config File [ManagedFileTransfer]"

            remote_name: str = Field(
                title="Remote Name",
                description="The name of the remote connection in the RClone configuration.",
            )
            config_file_contents: str = Field(
                title="Config File Contents",
                description="The contents of the RClone configuration file.",
            )

        _OWN_BLOCK = RCloneConfigFileBlock
    return _OWN_BLOCK


def _missing(error: Exception) -> bool:
    from prefect.exceptions import ObjectNotFound

    return isinstance(error, ObjectNotFound) or (
        isinstance(error, ValueError) and "Unable to find block document" in str(error)
    )


class PrefectConfigBlockStore:
    """Loads and saves rclone configs held in ``RCloneConfigFileBlock`` documents."""

    def _load_block(self, name: str) -> Any:
        try:
            return config_block_class().load(name, _sync=True)
        except Exception as e:
            if _missing(e):
                raise LookupError(name) from e
            raise

    def load(self, name: str) -> str:
        """The config text of block ``name``.

        Raises:
            LookupError: the block does not exist.
        """
        return str(self._load_block(name).config_file_contents)

    def save(self, name: str, contents: str) -> None:
        """Replace the config text of block ``name`` (a refreshed token)."""
        block = self._load_block(name)
        block.config_file_contents = contents
        block.save(name, overwrite=True, _sync=True)


@contextmanager
def prefect_lock(names: list[str], timeout: float | None) -> Iterator[None]:
    """Hold one slot of each Prefect global concurrency limit in ``names`` (limit 1).

    Missing limits are created with one slot first, so no setup is needed.

    Raises:
        TimeoutError: the slots were not free within ``timeout`` seconds.
    """
    for name in names:
        ensure_concurrency_limit(name, 1)
    with hp_concurrency(list(names), timeout_seconds=timeout, strict=True):
        yield


class _ProblemCache:
    """Remembers config lookups of the problem check for :data:`PROBLEM_CACHE_TTL`."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.entries: dict[str, tuple[float, set[str] | str]] = {}

    def remotes(self, source: rclone.ConfigSource) -> set[str] | str:
        """The remote names of ``source``, or a problem string."""
        now = time.monotonic()
        with self.lock:
            hit = self.entries.get(source.lock_name)
            if hit is not None and now - hit[0] < PROBLEM_CACHE_TTL:
                return hit[1]
        try:
            text = load_config(source, rclone.manager.block_store)
            result: set[str] | str = set(parse_sections(text, source.describe()))
        except (rclone.RcloneError, ValueError) as e:
            result = str(e)
        with self.lock:
            self.entries[source.lock_name] = (now, result)
        return result

    def clear(self) -> None:
        with self.lock:
            self.entries.clear()


_cache = _ProblemCache()


def rclone_problems(url: str, options: dict[str, Any]) -> list[str]:
    """Why an rclone storage location cannot be used now (empty when it can).

    Registered with :func:`heliopuddle.storage.credentials.register_problem_check`.
    """
    if not backends.server_only(url):
        return []
    try:
        rclone.find_rclone()
    except rclone.RcloneUnavailable as e:
        return [str(e)]
    try:
        source = rclone.source_from_options(options)
    except rclone.RcloneConfigError as e:
        return [str(e)]
    remotes = _cache.remotes(source)
    if isinstance(remotes, str):
        return [remotes]
    remote = url.split("://", 1)[1].split("/", 1)[0]
    if remote not in remotes:
        return [f"rclone remote {remote!r} is not defined in {source.describe()}"]
    return []


def install_rclone(settings: Settings, *, replace: bool = True) -> None:
    """Configure the rclone backend for this process.

    Args:
        settings: process settings (binary, download, idle).
        replace: re-install over an earlier install (False keeps it, e.g. in a
            flow-run process that already has it, or a test's hooks).
    """
    global _installed
    with _install_lock:
        if _installed and not replace:
            return
        rclone.configure(
            binary=settings.rclone_binary,
            download_dir=settings.data_dir / "bin",
            auto_download=settings.rclone_auto_download,
            idle=settings.rclone_idle.total_seconds(),
        )
        rclone.set_session_hooks(prefect_lock, PrefectConfigBlockStore())
        unregister_problem_check(rclone_problems)
        register_problem_check(rclone_problems)
        _cache.clear()
        _installed = True


def register_rclone_block_type() -> bool:
    """Register the ``RCloneConfigFileBlock`` type with Prefect (once Prefect is up).

    Defining or loading the class does not register it, so on a fresh Prefect
    server the block could not be created in the Prefect UI. A failure is
    logged and never stops startup: rclone storage locations then stay
    incomplete until the block exists.

    Returns:
        True if the block type is registered.
    """
    try:
        config_block_class().register_type_and_schema(_sync=True)
    except Exception as e:
        logger.warning(
            "Could not register the rclone config block type with Prefect "
            "(create it from Python instead): %s: %s",
            type(e).__name__,
            e,
        )
        return False
    return True


def close_rclone_sessions(timeout: float = 30.0) -> bool:
    """Close the open rclone session now, saving refreshed tokens (call before Prefect stops).

    Config blocks are saved through Prefect, so this must run while Prefect is
    still up. A session that does not close in time, or a config that could
    not be saved, is logged as an error: a refreshed token may be lost and the
    remote may need reconnecting.

    Returns:
        True if no session is open and every changed config was saved.
    """
    session = rclone.manager.session
    try:
        closed = rclone.manager.close(timeout, force=True)
    except Exception as e:
        logger.error("Closing rclone sessions failed: %s: %s", type(e).__name__, e)
        return False
    if not closed:
        logger.error(
            "rclone sessions did not close within %.0fs; a refreshed token may not have "
            "been saved to its config",
            timeout,
        )
        return False
    errors = list(session.writeback_errors) if session is not None else []
    for error in errors:
        logger.error(
            "rclone config not saved on shutdown (reconnect the remote if it fails): %s", error
        )
    return not errors

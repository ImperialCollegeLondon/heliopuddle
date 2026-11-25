"""The Prefect-backed block resolver of :mod:`heliopuddle.storage.credentials` (STO-18, STO-19).

Installed by ``server start`` once Prefect is up, and by flow-run processes
when they build their context. Lookups happen lazily, when a storage location
that uses blocks is opened, and are cached by the credentials module.
"""

from __future__ import annotations

from typing import Any

from heliopuddle.storage.credentials import get_block_resolver, set_block_resolver

__all__ = ["PrefectBlockResolver", "install_block_resolver"]


def _missing(error: Exception) -> bool:
    from prefect.exceptions import ObjectNotFound

    return isinstance(error, ObjectNotFound) or (
        isinstance(error, ValueError) and "Unable to find block document" in str(error)
    )


class PrefectBlockResolver:
    """Loads Prefect ``Secret`` and ``RemoteFileSystem`` blocks with the sync API."""

    def secret(self, name: str) -> Any:
        """The value of the Secret block ``name``.

        Raises:
            LookupError: the block does not exist.
        """
        from prefect.blocks.system import Secret

        try:
            block = Secret.load(name, _sync=True)  # pyright: ignore[reportCallIssue]
        except Exception as e:
            if _missing(e):
                raise LookupError(name) from e
            raise
        return block.get()

    def remote_file_system(self, name: str) -> tuple[str, dict[str, Any]]:
        """The ``basepath`` and ``settings`` of the RemoteFileSystem block ``name``.

        Raises:
            LookupError: the block does not exist.
        """
        from prefect.filesystems import RemoteFileSystem

        try:
            block = RemoteFileSystem.load(name, _sync=True)  # pyright: ignore[reportCallIssue]
        except Exception as e:
            if _missing(e):
                raise LookupError(name) from e
            raise
        return block.basepath, dict(block.settings or {})


def install_block_resolver(*, replace: bool = True) -> None:
    """Install :class:`PrefectBlockResolver` for this process.

    Args:
        replace: replace a resolver already installed (False keeps it, e.g. a
            test's fake).
    """
    if replace or get_block_resolver() is None:
        set_block_resolver(PrefectBlockResolver())

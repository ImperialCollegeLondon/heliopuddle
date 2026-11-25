"""Exceptions of the rclone storage backend. See docs/design/storage.md ("rclone")."""

from __future__ import annotations

__all__ = [
    "RcloneBusyError",
    "RcloneConfigError",
    "RcloneError",
    "RcloneUnavailable",
    "RcloneVerifyError",
]


class RcloneError(OSError):
    """An rclone remote control call failed (any error rcd reports that has no better match)."""


class RcloneUnavailable(RcloneError):
    """rclone cannot be used here: no binary, a missing config block, or rcd did not start."""


class RcloneConfigError(ValueError):
    """An rclone config is invalid, e.g. two configs of one session define the same remote."""


class RcloneBusyError(TimeoutError):
    """The lock of an rclone config was not free within the allowed wait (STO-20)."""


class RcloneVerifyError(RcloneError):
    """A written object does not match its spool (``verify`` ``native`` or ``size``)."""

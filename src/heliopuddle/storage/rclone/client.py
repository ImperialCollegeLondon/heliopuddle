"""A minimal client for the remote control API of a local ``rclone rcd`` (STO-22).

Only an allowlist of endpoints can be called (:data:`ALLOWED_ENDPOINTS`), plus
ranged ``GET`` reads from ``--rc-serve``. rcd listens on a Unix socket (or, where
there are none, on loopback TCP) and always requires the per-session user and
password, which never appear in ``repr``, errors or logs.
"""

from __future__ import annotations

import errno
import ipaddress
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from heliopuddle.storage.rclone.errors import RcloneConfigError, RcloneError

__all__ = ["ALLOWED_ENDPOINTS", "RcClient"]

#: The only remote control endpoints hp ever calls (STO-22).
ALLOWED_ENDPOINTS: frozenset[str] = frozenset(
    {
        *(
            f"operations/{name}"
            for name in (
                "list",
                "stat",
                "about",
                "hashsum",
                "copyfile",
                "movefile",
                "deletefile",
                "mkdir",
                "rmdir",
                "purge",
            )
        ),
        "config/listremotes",
        "core/version",
        "core/quit",
    }
)

_LOOPBACK_NAMES = frozenset({"localhost"})


def _is_loopback(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _error_text(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text.strip()[:500] or response.reason_phrase
    if isinstance(body, dict) and "error" in body:
        return str(body["error"])  # pyright: ignore[reportUnknownArgumentType]
    return response.reason_phrase


def error_for(status: int, what: str, text: str) -> Exception:
    """Map an rcd error (HTTP status and error text) to a Python exception.

    Args:
        status: the HTTP status rcd answered with.
        what: the endpoint or path, for the message.
        text: rcd's error text.

    Returns:
        ``FileNotFoundError`` for a missing object or directory,
        ``FileExistsError``, ``OSError(ENOTEMPTY)``, :class:`RcloneConfigError`
        for an unknown remote, otherwise :class:`RcloneError`.
    """
    message = f"rclone {what}: {text}"
    lowered = text.lower()
    if status == 401:
        return RcloneError(errno.EACCES, f"rclone {what}: rcd rejected the session credentials")
    if "didn't find section in config file" in lowered:
        return RcloneConfigError(message)
    if status == 404 or "no such file or directory" in lowered or "not found" in lowered:
        return FileNotFoundError(errno.ENOENT, message)
    if "directory not empty" in lowered:
        return OSError(errno.ENOTEMPTY, message)
    if "already exists" in lowered:
        return FileExistsError(errno.EEXIST, message)
    return RcloneError(errno.EIO, message)


class RcClient:
    """HTTP client for one rcd.

    Args:
        user: the session's rc user.
        password: the session's rc password.
        socket_path: the Unix socket rcd listens on, or
        url: a loopback ``http://127.0.0.1:<port>`` URL where there are no Unix sockets.
        connect_timeout: seconds to wait for a connection.

    Raises:
        ValueError: neither or both of ``socket_path`` and ``url``, or a
            non-loopback ``url``.
    """

    def __init__(
        self,
        *,
        user: str,
        password: str,
        socket_path: str | None = None,
        url: str | None = None,
        connect_timeout: float = 10.0,
    ) -> None:
        if (socket_path is None) == (url is None):
            raise ValueError("give exactly one of socket_path and url")
        if url is not None and not _is_loopback(url):
            raise ValueError("rcd is only ever reached on loopback")
        self._where = socket_path or url or ""
        transport = httpx.HTTPTransport(uds=socket_path) if socket_path else httpx.HTTPTransport()
        self._http = httpx.Client(
            base_url=url or "http://rclone",
            transport=transport,
            auth=httpx.BasicAuth(user, password),
            timeout=httpx.Timeout(None, connect=connect_timeout),
            trust_env=False,
        )

    def __repr__(self) -> str:
        return f"RcClient({self._where!r})"

    def call(self, endpoint: str, **params: Any) -> dict[str, Any]:
        """Call an allowed rc endpoint with JSON parameters and return its JSON result.

        Raises:
            PermissionError: ``endpoint`` is not in :data:`ALLOWED_ENDPOINTS`.
            FileNotFoundError, FileExistsError, OSError, RcloneConfigError,
            RcloneError: rcd reported an error (see :func:`error_for`).
        """
        if endpoint not in ALLOWED_ENDPOINTS:
            raise PermissionError(f"rclone rc endpoint not allowed: {endpoint!r}")
        try:
            response = self._http.post(f"/{endpoint}", json=params)
        except httpx.HTTPError as e:
            raise RcloneError(errno.EIO, f"rclone {endpoint}: {type(e).__name__}: {e}") from None
        if response.status_code != 200:
            raise error_for(response.status_code, endpoint, _error_text(response))
        result = response.json()
        return result if isinstance(result, dict) else {}  # pyright: ignore[reportUnknownVariableType]

    def read_range(self, fs: str, remote: str, start: int, end: int) -> bytes:
        """Read bytes ``[start, end)`` of ``fs`` + ``remote`` from ``--rc-serve``.

        Args:
            fs: an rclone fs spec such as ``remote:``.
            remote: the object's path within ``fs``.
            start: first byte.
            end: one past the last byte.

        Raises:
            FileNotFoundError: the object does not exist.
            RcloneError: any other failure.
        """
        if end <= start:
            return b""
        url = "/" + quote(f"[{fs}]/{remote}", safe="/[]:")
        what = f"read {fs}{remote}"
        try:
            response = self._http.get(url, headers={"Range": f"bytes={start}-{end - 1}"})
        except httpx.HTTPError as e:
            raise RcloneError(errno.EIO, f"rclone {what}: {type(e).__name__}: {e}") from None
        if response.status_code == 416:
            return b""
        if response.status_code == 200:
            return response.content[start:end]  # the server ignored the range
        if response.status_code != 206:
            raise error_for(response.status_code, what, _error_text(response))
        return response.content

    def close(self) -> None:
        """Close the HTTP connections."""
        self._http.close()

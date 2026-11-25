"""Starting and stopping a private ``rclone rcd`` (STO-22).

rcd runs with a random user and password (passed in its environment, which
only its owner can read, not on the command line, which every local user can
see), listens on a Unix socket in the session's 0700 directory (loopback TCP
where there are none), in its own process group, with a parent-death signal
on Linux, and is killed when hp exits.
"""

from __future__ import annotations

import atexit
import contextlib
import ctypes
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from heliopuddle.storage.rclone.client import RcClient
from heliopuddle.storage.rclone.errors import RcloneError, RcloneUnavailable

__all__ = ["Rcd", "kill_all", "start_rcd"]

_SOCKET_NAME = "rc.sock"
_MAX_SOCKET_PATH = 100  # sockaddr_un.sun_path is 104-108 bytes
_PR_SET_PDEATHSIG = 1

_live: set[subprocess.Popen[bytes]] = set()
_live_lock = threading.Lock()


def _parent_death_signal() -> None:  # pragma: no cover - runs in the child after fork
    """Ask Linux to kill rcd when the thread that started it dies (best effort)."""
    with contextlib.suppress(Exception):
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _use_unix_socket(directory: Path) -> bool:
    return hasattr(socket, "AF_UNIX") and len(str(directory / _SOCKET_NAME)) <= _MAX_SOCKET_PATH


@dataclass
class Rcd:
    """A running rcd and the client that talks to it.

    Attributes:
        process: the rcd process.
        client: the authenticated rc client.
        log_path: rcd's output (in the session directory).
    """

    process: subprocess.Popen[bytes]
    client: RcClient
    log_path: Path
    _stopped: bool = field(default=False, repr=False)

    def stop(self, grace: float = 5.0) -> None:
        """Stop rcd (SIGTERM, which rclone handles gracefully), killing it if it lingers.

        ``core/quit`` is used only where there are no POSIX signals: rcd takes
        over a second to exit after it, against milliseconds for SIGTERM.
        """
        if self._stopped:
            return
        self._stopped = True
        try:
            if os.name == "posix":
                _signal(self.process, signal.SIGTERM)
            else:  # pragma: no cover - Windows
                with contextlib.suppress(Exception):
                    self.client.call("core/quit")
            try:
                self.process.wait(grace)
            except subprocess.TimeoutExpired:
                _signal(self.process, getattr(signal, "SIGKILL", signal.SIGTERM))
                self.process.wait()
        finally:
            self.client.close()
            with _live_lock:
                _live.discard(self.process)


def _signal(process: subprocess.Popen[bytes], sig: int) -> None:
    with contextlib.suppress(OSError):
        if os.name == "posix":
            os.killpg(process.pid, sig)
        else:  # pragma: no cover - Windows
            process.kill()


def _log_tail(path: Path) -> str:
    try:
        lines = path.read_text(errors="replace").strip().splitlines()
    except OSError:
        return ""
    return lines[-1][:300] if lines else ""


def start_rcd(binary: str, config: Path, directory: Path, timeout: float = 30.0) -> Rcd:
    """Start ``rclone rcd --rc-serve`` on ``config`` and wait until it answers.

    Args:
        binary: the rclone executable.
        config: the session's temp config (0600).
        directory: the session's private (0700) directory, for the socket and log.
        timeout: seconds to wait for rcd to answer.

    Raises:
        RcloneUnavailable: rcd exited or did not answer in time.
    """
    user, password = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    if _use_unix_socket(directory):
        sock = str(directory / _SOCKET_NAME)
        addr, url = f"unix://{sock}", None
    else:  # pragma: no cover - platforms without Unix sockets or very long temp paths
        sock = None
        port = _free_loopback_port()
        addr, url = f"127.0.0.1:{port}", f"http://127.0.0.1:{port}"
    # Never --rc-no-auth, --rc-web-gui or metrics; no inherited RCLONE_* setting
    # (such as RCLONE_RC_NO_AUTH) can loosen that.
    command = [binary, "rcd", "--config", str(config), "--rc-serve", "--rc-addr", addr]
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("RCLONE_")}
    env |= {"RCLONE_RC_USER": user, "RCLONE_RC_PASS": password}
    log_path = directory / "rcd.log"
    posix = os.name == "posix"
    with log_path.open("wb") as log:
        try:
            process = subprocess.Popen(
                command,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=posix,
                preexec_fn=_parent_death_signal if sys.platform.startswith("linux") else None,
            )
        except OSError as e:
            raise RcloneUnavailable(f"cannot start rclone {binary!r}: {e.strerror or e}") from None
    with _live_lock:
        _live.add(process)
    rcd = Rcd(process, RcClient(user=user, password=password, socket_path=sock, url=url), log_path)
    deadline = time.monotonic() + timeout
    while True:
        code = process.poll()
        if code is not None:
            rcd.stop(0)
            raise RcloneUnavailable(f"rclone rcd exited with code {code}: {_log_tail(log_path)}")
        try:
            rcd.client.call("core/version")
            return rcd
        except RcloneError:
            if time.monotonic() > deadline:
                rcd.stop(0)
                raise RcloneUnavailable(f"rclone rcd did not answer within {timeout:g}s") from None
            time.sleep(0.02)


def kill_all() -> None:
    """Kill every rcd this process started that is still running (atexit)."""
    with _live_lock:
        processes = list(_live)
        _live.clear()
    for process in processes:
        _signal(process, getattr(signal, "SIGKILL", signal.SIGTERM))


atexit.register(kill_all)

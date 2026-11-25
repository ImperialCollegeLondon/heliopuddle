"""Find the rclone binary, or download the pinned release (DEP-21).

Order: the configured ``binary`` (``HELIOPUDDLE_RCLONE_BINARY``), then
``rclone`` on ``PATH``, then a previously downloaded copy, then (unless
``auto_download`` is off) a download of :data:`RCLONE_VERSION` from
``downloads.rclone.org`` into ``<download dir>/rclone-v<version>/``. The
archive's SHA256 is checked against values pinned here (from the release's
PGP-signed ``SHA256SUMS``), so no gpg is needed.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import platform
import shutil
import ssl
import tempfile
import threading
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

import httpx

from heliopuddle.storage.rclone.errors import RcloneUnavailable

__all__ = [
    "DEFAULT_BASE_URL",
    "RCLONE_SHA256",
    "RCLONE_VERSION",
    "BinarySettings",
    "configure_binary",
    "download_rclone",
    "find_rclone",
    "platform_key",
]

RCLONE_VERSION = "1.76.0"
DEFAULT_BASE_URL = "https://downloads.rclone.org"

#: SHA256 of ``rclone-v<version>-<os>-<arch>.zip`` from the release's SHA256SUMS.
RCLONE_SHA256: Mapping[str, str] = {
    "linux-amd64": "b00f00b3266c3764413afe599d3ec316f6f43b3e0ea8a0bc1d186d2133e46df7",
    "linux-arm64": "ee169016675e23a69d77240565133ff7dbc9b33d16fa8f7053d4af39f1eaeb78",
    "osx-amd64": "4f5835733d52b818340f97b1fc0348a70461534d123363a183ca2b660219595a",
    "osx-arm64": "e72dc90b70f4947a7b72b61bde4a4fbba5777b2874fb448951ffdde67ebd0ab7",
    "windows-amd64": "8b4fd2737ade4e70c057ef4a49deb7cffe94b0fb9d7587c189d149823f21a1dc",
    "windows-arm64": "c406c8a69a5a408e50624aa990fbb0a6266b3a4e83782a94de3cb96f50f86f10",
}

_SYSTEMS = {"linux": "linux", "darwin": "osx", "windows": "windows"}
_MACHINES = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}


def _default_download_dir() -> Path:
    cache = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(cache) / "heliopuddle" / "bin"


@dataclass(frozen=True)
class BinarySettings:
    """Where rclone comes from.

    Attributes:
        binary: an explicit rclone executable, used before anything else.
        download_dir: where the pinned release is downloaded (``<data dir>/bin``
            on a server); defaults to ``~/.cache/heliopuddle/bin``.
        auto_download: download the pinned release when no rclone is found.
        base_url: the download site (tests point it at a local server).
    """

    binary: str | None = None
    download_dir: Path | None = None
    auto_download: bool = True
    base_url: str = DEFAULT_BASE_URL


_lock = threading.Lock()
_settings = BinarySettings()
_found: str | None = None


def configure_binary(**changes: object) -> BinarySettings:
    """Change :class:`BinarySettings` fields (``binary``, ``download_dir``, ...); returns them."""
    global _settings, _found
    with _lock:
        _settings = replace(_settings, **changes)  # type: ignore[arg-type]
        _found = None
        return _settings


def platform_key(system: str | None = None, machine: str | None = None) -> str:
    """The release's ``<os>-<arch>`` name for this machine, e.g. ``linux-amd64``.

    Raises:
        RcloneUnavailable: no pinned release exists for this OS and architecture.
    """
    sys_name = (system or platform.system()).lower()
    arch = (machine or platform.machine()).lower()
    key = f"{_SYSTEMS.get(sys_name, sys_name)}-{_MACHINES.get(arch, arch)}"
    if key not in RCLONE_SHA256:
        raise RcloneUnavailable(f"no pinned rclone release for {key}: install rclone on PATH")
    return key


def _executable_name(key: str) -> str:
    return "rclone.exe" if key.startswith("windows") else "rclone"


def find_rclone() -> str:
    """Return the path of the rclone executable to use, downloading it if allowed.

    Raises:
        RcloneUnavailable: the configured binary does not exist, or no rclone
            was found and downloading is off or failed.
    """
    global _found
    with _lock:
        if _found is not None:
            return _found
        settings = _settings
        if settings.binary:
            if not (os.path.isfile(settings.binary) and os.access(settings.binary, os.X_OK)):
                raise RcloneUnavailable(f"rclone binary {settings.binary!r} is not an executable")
            _found = settings.binary
            return _found
        on_path = shutil.which("rclone")
        if on_path:
            _found = on_path
            return _found
        directory = settings.download_dir or _default_download_dir()
        key = platform_key()
        installed = directory / f"rclone-v{RCLONE_VERSION}" / _executable_name(key)
        if not installed.is_file():
            if not settings.auto_download:
                raise RcloneUnavailable(
                    "rclone was not found: set HELIOPUDDLE_RCLONE_BINARY, put rclone on "
                    "PATH, or allow HELIOPUDDLE_RCLONE_AUTO_DOWNLOAD"
                )
            installed = download_rclone(directory, base_url=settings.base_url, key=key)
        _found = str(installed)
        return _found


def _fetch(url: str, target: Path) -> str:
    """Download ``url`` into ``target``; return its SHA256."""
    digest = hashlib.sha256()
    context = ssl.create_default_context()  # the system CA store (and SSL_CERT_FILE)
    with (
        httpx.Client(verify=context, follow_redirects=True, timeout=60.0) as client,
        client.stream("GET", url) as response,
        target.open("wb") as out,
    ):
        response.raise_for_status()
        for chunk in response.iter_bytes(1 << 20):
            digest.update(chunk)
            out.write(chunk)
    return digest.hexdigest()


def download_rclone(
    directory: Path,
    *,
    base_url: str = DEFAULT_BASE_URL,
    key: str | None = None,
    sha256: Mapping[str, str] = RCLONE_SHA256,
) -> Path:
    """Download and install the pinned rclone into ``directory/rclone-v<version>/``.

    The archive is checked against ``sha256`` before anything is extracted; the
    executable is installed by an atomic rename, so a concurrent or interrupted
    download never leaves a partial binary.

    Args:
        directory: the download directory (created if missing).
        base_url: the download site.
        key: ``<os>-<arch>`` (default: this machine).
        sha256: pinned archive hashes by key.

    Returns:
        The installed executable.

    Raises:
        RcloneUnavailable: the download failed or the archive's hash is wrong.
    """
    key = key or platform_key()
    name = f"rclone-v{RCLONE_VERSION}-{key}"
    final_dir = directory / f"rclone-v{RCLONE_VERSION}"
    executable = _executable_name(key)
    url = f"{base_url.rstrip('/')}/v{RCLONE_VERSION}/{name}.zip"
    directory.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".rclone-download-", dir=directory))
    try:
        archive = work / "rclone.zip"
        try:
            actual = _fetch(url, archive)
        except (httpx.HTTPError, OSError) as e:
            raise RcloneUnavailable(f"downloading rclone from {url} failed: {e}") from None
        if actual != sha256.get(key):
            raise RcloneUnavailable(
                f"rclone download {url} has SHA256 {actual}, expected {sha256.get(key)}"
            )
        staged = work / "install"
        staged.mkdir()
        try:
            with zipfile.ZipFile(archive) as zf:
                data = zf.read(f"{name}/{executable}")
        except (KeyError, zipfile.BadZipFile) as e:
            raise RcloneUnavailable(f"rclone archive {url} is not as expected: {e}") from None
        (staged / executable).write_bytes(data)
        os.chmod(staged / executable, 0o755)
        try:
            os.replace(staged, final_dir)
        except OSError:
            if not (final_dir / executable).is_file():  # not a concurrent install
                raise
        return final_dir / executable
    finally:
        with contextlib.suppress(OSError):
            shutil.rmtree(work)

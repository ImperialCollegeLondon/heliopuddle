"""STO-17: SFTP storage locations through fsspec's own ``sftp://`` (paramiko).

The STO-07 conformance suite runs unchanged against an OpenSSH server in a
container, plus the SFTP facts :mod:`heliopuddle.storage.backends` relies on:
a move atomically replaces an existing destination
(``posix-rename@openssh.com``), free space comes from the
``statvfs@openssh.com`` extension, ``create_root: auto`` creates a missing
root directory, and both password and public-key authentication work.

``HP_SFTP_IMAGE`` overrides the image (for example to use a registry mirror).
"""

from __future__ import annotations

import contextlib
import io
import os
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import paramiko
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fsspec.implementations.sftp import SFTPFileSystem

from heliopuddle.storage import StorageLocation, backends
from heliopuddle.storage.location import CreateRoot
from tests.integration.conftest import STARTUP_TIMEOUT_S, _host_ip, require_docker
from tests.storage_conformance import (
    LocationFactory,
    MissingRootFactory,
    StorageConformance,
    _md5,
)

pytestmark = pytest.mark.req("STO-17")

SFTP_IMAGE = os.environ.get("HP_SFTP_IMAGE", "lscr.io/linuxserver/openssh-server:10.3_p1-r2-ls244")
SFTP_PORT = 2222
SFTP_USER = "hp"
SFTP_PASSWORD = "hp-sftp-secret"
# The user's home directory in the image; every test uses a unique root below it.
SFTP_HOME = "/config"


@dataclass(frozen=True)
class SftpServer:
    """Where the test OpenSSH server listens and how to log in."""

    host: str
    port: int
    key_file: Path

    def password_options(self) -> dict[str, Any]:
        """``storage_options`` for password authentication."""
        return {
            "host": self.host,
            "port": self.port,
            "username": SFTP_USER,
            "password": SFTP_PASSWORD,
            "look_for_keys": False,
            "allow_agent": False,
        }

    def key_options(self) -> dict[str, Any]:
        """``storage_options`` for public-key authentication (no password)."""
        return {
            "host": self.host,
            "port": self.port,
            "username": SFTP_USER,
            "key_filename": str(self.key_file),
            "look_for_keys": False,
            "allow_agent": False,
        }

    def url(self, path: str) -> str:
        """An ``sftp://`` URL for an absolute server path."""
        return f"sftp://{self.host}:{self.port}{path}"

    def fs(self) -> SFTPFileSystem:
        """A fresh connection, independent of the storage location under test."""
        options = self.password_options()
        return SFTPFileSystem(options.pop("host"), skip_instance_cache=True, **options)


def _write_ed25519_key(directory: Path) -> tuple[Path, str]:
    """Write a new ed25519 private key in OpenSSH format; return it and its public line."""
    private = Ed25519PrivateKey.generate()
    key_file = directory / "id_ed25519"
    key_file.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        )
    )
    key_file.chmod(0o600)
    public = paramiko.Ed25519Key(filename=str(key_file))
    return key_file, f"{public.get_name()} {public.get_base64()} hp-test"


def _wait_for_sftp(host: str, port: int, key_file: Path, container: Any) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    while True:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            # Both methods must be ready: the key is installed after the password.
            auths: list[dict[str, Any]] = [
                {"password": SFTP_PASSWORD},
                {"key_filename": str(key_file)},
            ]
            for auth in auths:
                client.connect(
                    host,
                    port=port,
                    username=SFTP_USER,
                    look_for_keys=False,
                    allow_agent=False,
                    timeout=5,
                    **auth,
                )
                client.open_sftp().listdir(SFTP_HOME)
                client.close()
            return
        except Exception as e:
            if time.monotonic() > deadline:
                logs = b"\n".join(x for x in container.get_logs() if x).decode(errors="replace")
                pytest.fail(f"SFTP server not ready on port {port}: {e!r}\n{logs[-5000:]}")
            time.sleep(0.5)
        finally:
            client.close()


@pytest.fixture(scope="session")
def sftp_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SftpServer]:
    """An OpenSSH server (``HP_SFTP_IMAGE``) with password and public-key logins."""
    require_docker()
    from testcontainers.core.container import DockerContainer

    key_file, public_line = _write_ed25519_key(tmp_path_factory.mktemp("sftp-key"))
    container = (
        DockerContainer(SFTP_IMAGE)
        .with_env("USER_NAME", SFTP_USER)
        .with_env("USER_PASSWORD", SFTP_PASSWORD)
        .with_env("PASSWORD_ACCESS", "true")
        .with_env("PUBLIC_KEY", public_line)
        .with_exposed_ports(SFTP_PORT)
    )
    container.start()
    try:
        host = _host_ip(container)
        port = int(container.get_exposed_port(SFTP_PORT))
        _wait_for_sftp(host, port, key_file, container)
        yield SftpServer(host, port, key_file)
    finally:
        container.stop()


def _unique_dir(prefix: str = "hp") -> str:
    return f"{SFTP_HOME}/{prefix}-{uuid.uuid4().hex[:12]}"


class TestSftpConformance(StorageConformance):
    object_store = False

    pytestmark = pytest.mark.req("STO-07")

    @pytest.fixture
    def location_factory(self, sftp_server: SftpServer) -> Iterator[LocationFactory]:
        made: list[StorageLocation] = []

        def make() -> StorageLocation:
            loc = StorageLocation(
                sftp_server.url(f"{_unique_dir()}/root/dir"),
                sftp_server.password_options(),
                create_root=True,
            )
            loc.ensure_root()  # a fresh, empty directory
            made.append(loc)
            return loc

        yield make
        for loc in made:
            with contextlib.suppress(Exception):
                loc.fs.rm(loc.root, recursive=True)

    @pytest.fixture
    def missing_root_factory(self, sftp_server: SftpServer) -> MissingRootFactory:
        def make(create_root: CreateRoot) -> tuple[StorageLocation, Callable[[], bool]]:
            root = f"{_unique_dir('hp-missing')}/root"
            loc = StorageLocation(
                sftp_server.url(root), sftp_server.password_options(), create_root
            )
            return loc, lambda: bool(sftp_server.fs().exists(root))

        return make


def test_sftp_location_uses_fsspec_sftp(sftp_server: SftpServer) -> None:
    loc = StorageLocation(sftp_server.url(_unique_dir()), sftp_server.password_options())

    assert isinstance(loc.fs, SFTPFileSystem)
    assert not backends.is_object_store(loc.fs.protocol)
    assert backends.local_path(loc.fs, loc.root) is None


def test_move_replaces_an_existing_destination_atomically(sftp_server: SftpServer) -> None:
    # fsspec's SFTP mv is posix-rename@openssh.com: it replaces the
    # destination, so move_replaces is True and no backup step is needed.
    loc = StorageLocation(
        sftp_server.url(_unique_dir()), sftp_server.password_options(), create_root=True
    )
    loc.write("m/final.txt", io.BytesIO(b"old"))
    staged = loc.write_staged("m/final.txt", "t", io.BytesIO(b"new"))

    assert backends.move_replaces(loc.fs.protocol) is True
    assert loc.move_replaces is True
    loc.move(staged, "m/final.txt")

    assert loc.md5("m/final.txt") == _md5(b"new")
    assert not loc.exists(staged)
    assert [e.path for e in loc.list_files()] == ["m/final.txt"]


def test_move_onto_a_directory_fails_and_keeps_both(sftp_server: SftpServer) -> None:
    loc = StorageLocation(
        sftp_server.url(_unique_dir()), sftp_server.password_options(), create_root=True
    )
    loc.write("d/inside.txt", io.BytesIO(b"inside"))
    loc.write("f.txt", io.BytesIO(b"file"))

    with pytest.raises(OSError):
        loc.move("f.txt", "d")

    assert loc.md5("f.txt") == _md5(b"file")
    assert loc.md5("d/inside.txt") == _md5(b"inside")


def test_free_space_from_statvfs(sftp_server: SftpServer) -> None:
    loc = StorageLocation(
        sftp_server.url(_unique_dir()), sftp_server.password_options(), create_root=True
    )
    loc.ensure_root()

    native = loc.free_space()

    assert isinstance(native, int) and native > 0
    assert loc.free_space(quota=native + 1_000, indexed_size=0) == native
    assert loc.free_space(quota=100, indexed_size=40) == 60


@pytest.mark.req("STO-08")
def test_create_root_auto_creates_a_missing_root(sftp_server: SftpServer) -> None:
    root = f"{_unique_dir('hp-auto')}/nested/root"
    loc = StorageLocation(sftp_server.url(root), sftp_server.password_options())
    assert loc.create_root == "auto" and loc.may_create_root
    assert not sftp_server.fs().exists(root)

    loc.ensure_root()

    assert sftp_server.fs().isdir(root)
    loc.ensure_root()  # idempotent once it exists
    loc.write("f.txt", io.BytesIO(b"f"))
    assert [e.path for e in loc.list_files()] == ["f.txt"]


def test_public_key_authentication(sftp_server: SftpServer, tmp_path: Path) -> None:
    options = sftp_server.key_options()
    assert "password" not in options
    loc = StorageLocation(sftp_server.url(_unique_dir("hp-key")), options)
    src = tmp_path / "k.bin"
    src.write_bytes(b"key auth\n" * 1000)

    loc.write("k/k.bin", src)

    assert loc.md5("k/k.bin") == _md5(src.read_bytes())
    assert sftp_server.fs().cat_file(loc.full_path("k/k.bin")) == src.read_bytes()


def test_wrong_key_is_refused(sftp_server: SftpServer, tmp_path: Path) -> None:
    other_key, _ = _write_ed25519_key(tmp_path)
    options = sftp_server.key_options() | {"key_filename": str(other_key)}

    with pytest.raises(paramiko.AuthenticationException):
        StorageLocation(sftp_server.url(_unique_dir()), options | {"skip_instance_cache": True})


def test_password_authentication_and_wrong_password(sftp_server: SftpServer) -> None:
    loc = StorageLocation(sftp_server.url(_unique_dir("hp-pw")), sftp_server.password_options())
    loc.write("p.txt", io.BytesIO(b"password auth"))
    assert loc.md5("p.txt") == _md5(b"password auth")

    bad = sftp_server.password_options() | {"password": "wrong", "skip_instance_cache": True}
    with pytest.raises(paramiko.AuthenticationException):
        StorageLocation(sftp_server.url(_unique_dir()), bad)

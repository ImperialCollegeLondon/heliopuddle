"""STO-05, STO-07, STO-22, DEP-21: rclone storage locations with the real rclone binary.

The conformance suite runs through rclone's ``webdav`` backend against a
WebDAV server container (``rclone serve webdav``; with ``vendor = other`` no
hashes and no streamed upload, like OneDrive). ``verify: native`` is checked
on a ``local`` alias remote, which reports hashes. The pinned rclone release is
downloaded once (its SHA256 checked) into ``~/.cache/heliopuddle-test``.

``HP_RCLONE_IMAGE`` overrides the WebDAV server image.
"""

from __future__ import annotations

import io
import os
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

from heliopuddle.storage import StorageLocation, backends
from heliopuddle.storage.location import CreateRoot
from heliopuddle.storage.rclone import (
    RcloneVerifyError,
    binary,
    configure,
    manager,
    source_from_options,
)
from tests.integration.conftest import STARTUP_TIMEOUT_S, _host_ip, require_docker
from tests.storage_conformance import LocationFactory, MissingRootFactory, StorageConformance, _md5

RCLONE_IMAGE = os.environ.get("HP_RCLONE_IMAGE", f"rclone/rclone:{binary.RCLONE_VERSION}")
DAV_PORT = 8080
DAV_USER = "hp"
DAV_PASSWORD = "hp-webdav-secret"
CACHE = Path.home() / ".cache" / "heliopuddle-test" / "bin"


@pytest.fixture(scope="session")
def rclone_binary() -> str:
    """The pinned rclone, downloaded once with its SHA256 checked (DEP-21)."""
    installed = CACHE / f"rclone-v{binary.RCLONE_VERSION}" / "rclone"
    if not installed.is_file():
        binary.download_rclone(CACHE)
    return str(installed)


@pytest.fixture(scope="session")
def webdav() -> Iterator[str]:
    """A WebDAV server (``rclone serve webdav``) for the session; yields its URL."""
    require_docker()
    from testcontainers.core.container import DockerContainer

    container = (
        DockerContainer(RCLONE_IMAGE)
        .with_command(
            f"serve webdav /data --addr :{DAV_PORT} --user {DAV_USER} --pass {DAV_PASSWORD}"
        )
        .with_exposed_ports(DAV_PORT)
    )
    container.start()
    try:
        url = f"http://{_host_ip(container)}:{container.get_exposed_port(DAV_PORT)}"
        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        while True:
            try:
                if httpx.request("PROPFIND", url, auth=(DAV_USER, DAV_PASSWORD)).status_code < 500:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                pytest.fail(f"WebDAV server not ready at {url}")
            time.sleep(0.2)
        yield url
    finally:
        container.stop()


@pytest.fixture(autouse=True)
def _use_rclone(rclone_binary: str) -> Iterator[None]:
    configure(binary=rclone_binary, idle=0.5)
    yield
    manager.close(30, force=True)
    configure(binary=None, idle=2.0)


@pytest.fixture
def dav_config(rclone_binary: str, webdav: str, tmp_path: Path) -> Path:
    """An rclone.conf with a ``webdav`` remote ``dav`` (password obscured, as rclone stores it)."""
    obscured = subprocess.run(
        [rclone_binary, "obscure", DAV_PASSWORD], capture_output=True, text=True, check=True
    ).stdout.strip()
    config = tmp_path / "rclone.conf"
    config.write_text(
        f"[dav]\ntype = webdav\nurl = {webdav}\nvendor = other\n"
        f"user = {DAV_USER}\npass = {obscured}\n"
    )
    config.chmod(0o600)
    return config


def _dav(
    config: Path, root: str, create_root: CreateRoot = True, **options: str
) -> StorageLocation:
    return StorageLocation(
        f"rclone://dav/{root}", {"config_file": str(config), **options}, create_root
    )


@pytest.mark.req("STO-07")
@pytest.mark.req("STO-05")
class TestRcloneWebdavConformance(StorageConformance):
    object_store = False

    @pytest.fixture
    def location_factory(self, dav_config: Path) -> LocationFactory:
        def make() -> StorageLocation:
            loc = _dav(dav_config, f"hp-{uuid.uuid4().hex[:12]}/a root")
            loc.ensure_root()
            return loc

        return make

    @pytest.fixture
    def missing_root_factory(self, dav_config: Path) -> MissingRootFactory:
        def make(create_root: CreateRoot) -> tuple[StorageLocation, Callable[[], bool]]:
            root = f"hp-missing-{uuid.uuid4().hex[:12]}/root"
            loc = _dav(dav_config, root, create_root)
            return loc, lambda: bool(loc.fs.exists(f"dav/{root}"))

        return make


@pytest.mark.req("DEP-21")
def test_pinned_release_runs(rclone_binary: str) -> None:
    out = subprocess.run([rclone_binary, "version"], capture_output=True, text=True, check=True)
    assert out.stdout.startswith(f"rclone v{binary.RCLONE_VERSION}")


@pytest.mark.req("STO-22")
def test_rcd_rejects_requests_without_credentials(dav_config: Path) -> None:
    source = source_from_options({"config_file": str(dav_config)})
    with manager.use([source]) as session:
        assert session.directory is not None
        sock = str(session.directory / "rc.sock")
        with httpx.Client(transport=httpx.HTTPTransport(uds=sock), base_url="http://rclone") as c:
            assert c.post("/core/version").status_code == 401
            assert c.post("/config/dump").status_code == 401
            assert c.get("/[dav:]/anything").status_code == 401
        assert session.client.call("core/version")["version"] == f"v{binary.RCLONE_VERSION}"


@pytest.mark.req("STO-05")
def test_auto_on_plain_webdav_reads_the_content(dav_config: Path) -> None:
    loc = _dav(dav_config, f"hp-{uuid.uuid4().hex[:12]}")
    loc.write("f.txt", io.BytesIO(b"payload"))
    assert backends.verified_md5(loc.fs, loc.full_path("f.txt")) is None
    assert loc.md5("f.txt") == _md5(b"payload")


@pytest.mark.req("STO-05")
def test_native_on_plain_webdav_fails_and_leaves_nothing(dav_config: Path) -> None:
    loc = _dav(dav_config, f"hp-{uuid.uuid4().hex[:12]}", verify="native")
    with pytest.raises(RcloneVerifyError, match="no hash"):
        loc.write("f.txt", io.BytesIO(b"payload"))
    assert not loc.exists("f.txt")


@pytest.mark.req("STO-05")
@pytest.mark.parametrize("verify", ["native", "auto", "size"])
def test_verify_on_a_remote_with_hashes(tmp_path: Path, verify: str) -> None:
    data = tmp_path / "data"
    data.mkdir()
    config = tmp_path / "rclone.conf"
    config.write_text(f"[disk]\ntype = alias\nremote = {data}\n")
    loc = StorageLocation(
        "rclone://disk/root", {"config_file": str(config), "verify": verify}, create_root=True
    )
    payload = b"native hash check\n" * 1000
    loc.write("dir/f.bin", io.BytesIO(payload))
    full = loc.full_path("dir/f.bin")
    assert backends.verified_md5(loc.fs, full) == _md5(payload)
    assert loc.md5("dir/f.bin") == _md5(payload)
    (data / "root" / "dir" / "f.bin").write_bytes(payload[:-1] + b"?")  # changed behind hp
    expected = _md5(payload) if verify == "size" else None  # size cannot tell
    assert backends.verified_md5(loc.fs, full) == expected
    assert loc.free_space() is not None  # the local backend reports free space

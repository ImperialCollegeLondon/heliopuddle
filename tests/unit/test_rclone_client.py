# ruff: noqa: F811
"""STO-22: the rc client calls only allowed endpoints, always with credentials."""

from __future__ import annotations

import errno
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from heliopuddle.storage.rclone import RcloneConfigError, RcloneError
from heliopuddle.storage.rclone.client import RcClient, error_for
from heliopuddle.storage.rclone.process import Rcd, start_rcd
from tests.unit._rclone_helpers import (  # noqa: F401 - fixtures
    RemoteFactory,
    fake_rclone,
    make_remote,
    short_tmp,
)


@pytest.fixture
def rcd(fake_rclone: Path, make_remote: RemoteFactory, short_tmp: Path) -> Iterator[Rcd]:
    config = make_remote()
    (Path(config.read_text().split("remote = ")[1].strip()) / "f.txt").write_bytes(b"0123456789")
    directory = short_tmp / "s"
    directory.mkdir(mode=0o700)
    started = start_rcd(str(fake_rclone), config, directory, timeout=20)
    yield started
    started.stop()


@pytest.mark.req("STO-22")
def test_endpoints_outside_the_allowlist_raise(rcd: Rcd) -> None:
    for endpoint in ("core/command", "config/dump", "sync/sync", "operations/check", "rc/noop"):
        with pytest.raises(PermissionError, match="not allowed"):
            rcd.client.call(endpoint)
    assert rcd.client.call("core/version")["version"]


@pytest.mark.req("STO-22")
def test_requests_without_or_with_wrong_credentials_get_401(rcd: Rcd) -> None:
    socket_path = str(rcd.log_path.parent / "rc.sock")
    transport = httpx.HTTPTransport(uds=socket_path)
    with httpx.Client(transport=transport, base_url="http://rclone") as anonymous:
        assert anonymous.post("/core/version").status_code == 401
        assert anonymous.get("/[remote:]/f.txt").status_code == 401
    wrong = RcClient(user="u", password="p", socket_path=socket_path)
    with pytest.raises(RcloneError, match="rejected the session credentials") as info:
        wrong.call("core/version")
    assert info.value.errno == errno.EACCES
    wrong.close()


@pytest.mark.req("STO-22")
def test_repr_hides_credentials() -> None:
    client = RcClient(user="the-user", password="the-pass", socket_path="/tmp/x.sock")
    assert "the-user" not in repr(client) and "the-pass" not in repr(client)
    client.close()


@pytest.mark.req("STO-22")
def test_only_loopback_tcp() -> None:
    with pytest.raises(ValueError, match="loopback"):
        RcClient(user="u", password="p", url="http://10.0.0.1:5572")
    with pytest.raises(ValueError, match="exactly one"):
        RcClient(user="u", password="p")
    for url in ("http://127.0.0.1:5572", "http://localhost:5572", "http://[::1]:5572"):
        RcClient(user="u", password="p", url=url).close()


@pytest.mark.req("STO-05")
def test_ranged_reads(rcd: Rcd) -> None:
    assert rcd.client.read_range("remote:", "f.txt", 2, 5) == b"234"
    assert rcd.client.read_range("remote:", "f.txt", 5, 5) == b""
    with pytest.raises(FileNotFoundError):
        rcd.client.read_range("remote:", "missing.txt", 0, 4)


@pytest.mark.req("STO-05")
def test_errors_map_to_python_exceptions(rcd: Rcd) -> None:
    with pytest.raises(FileNotFoundError):
        rcd.client.call("operations/deletefile", fs="remote:", remote="nope")
    with pytest.raises(RcloneConfigError):
        rcd.client.call("operations/stat", fs="nosuch:", remote="x")
    with pytest.raises(RcloneError, match="support about"):
        rcd.client.call("operations/about", fs="remote:")


@pytest.mark.parametrize(
    ("status", "text", "kind"),
    [
        (404, "object not found", FileNotFoundError),
        (500, "stat /x: no such file or directory", FileNotFoundError),
        (500, "remove /x: directory not empty", OSError),
        (500, "file already exists", FileExistsError),
        (500, 'didn\'t find section in config file ("x")', RcloneConfigError),
        (500, "something else", RcloneError),
    ],
)
def test_error_mapping(status: int, text: str, kind: type[Exception]) -> None:
    error = error_for(status, "operations/x", text)
    assert type(error) is kind
    assert "operations/x" in str(error)


def test_dead_rcd_is_an_rclone_error(rcd: Rcd) -> None:
    rcd.process.kill()
    rcd.process.wait(5)
    with pytest.raises(RcloneError):
        rcd.client.call("core/version")

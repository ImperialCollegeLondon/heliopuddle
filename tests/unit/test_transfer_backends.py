"""Native free-space queries and conservative same-filesystem capabilities (STO-14)."""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import fsspec
import pytest

from heliopuddle.storage import backends


@pytest.mark.req("STO-14")
def test_local_free_space_uses_existing_ancestor(tmp_path: Path) -> None:
    fs = fsspec.filesystem("file")
    native = shutil.disk_usage(tmp_path).free
    free = backends.free_space(fs, str(tmp_path / "not" / "created"))
    assert free is not None
    assert abs(native - free) < 10_000_000


@pytest.mark.req("STO-14")
@pytest.mark.parametrize("protocol", ["s3", "s3a", "gs", "gcs", "az", "abfs", "memory", "other"])
def test_other_backends_are_unlimited_without_quota(protocol: str) -> None:
    fs = SimpleNamespace(protocol=protocol)
    assert backends.free_space(fs, "container/prefix") is None


@pytest.mark.req("STO-14")
def test_smb_uses_volume_query_and_callers_available_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import smbclient

    calls: list[tuple[str, dict[str, Any]]] = []

    def stat_volume(path: str, **kwargs: Any) -> Any:
        calls.append((path, kwargs))
        return SimpleNamespace(
            total_size=1_000_000, caller_available_size=17_000, actual_available_size=31_000
        )

    monkeypatch.setattr(smbclient, "stat_volume", stat_volume)
    fs = SimpleNamespace(protocol="smb", host="server", port=1445)
    assert backends.free_space(fs, "/share/prefix") == 17_000
    assert calls == [(r"\\server\share\prefix", {"port": 1445})]


@pytest.mark.req("STO-14")
@pytest.mark.parametrize("protocol", ["sftp", "ssh"])
def test_sftp_sends_statvfs_extension_and_decodes_available_blocks(protocol: str) -> None:
    from paramiko.message import Message
    from paramiko.sftp import CMD_EXTENDED, CMD_EXTENDED_REPLY

    calls: list[tuple[Any, ...]] = []
    response = Message()
    for field in (4096, 1024, 8000, 6000, 5000, 100, 50, 40, 17, 0, 255):
        response.add_int64(field)

    def request(*args: Any) -> tuple[int, Message]:
        calls.append(args)
        return CMD_EXTENDED_REPLY, Message(response.asbytes())

    fs = SimpleNamespace(protocol=protocol, ftp=SimpleNamespace(_request=request))
    assert backends.free_space(fs, "/archive") == 5_000 * 1024
    assert calls == [(CMD_EXTENDED, "statvfs@openssh.com", "/archive")]


@pytest.mark.req("STO-14")
def test_native_query_failure_is_not_unlimited(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise PermissionError("native query failed")

    monkeypatch.setattr(shutil, "disk_usage", fail)
    with pytest.raises(PermissionError, match="native query failed"):
        backends.free_space(fsspec.filesystem("file"), "/data")


@pytest.mark.req("SVC-19")
def test_copy_capability_requires_compatible_connections_and_an_implementation() -> None:
    from fsspec.implementations.sftp import SFTPFileSystem

    first = fsspec.filesystem("memory", skip_instance_cache=True)
    second = fsspec.filesystem("memory", skip_instance_cache=True)
    local = fsspec.filesystem("file")
    assert backends.same_filesystem(first, second)
    assert backends.same_filesystem(local, local)
    assert not backends.same_filesystem(first, local)
    remote = object.__new__(SFTPFileSystem)
    assert not backends.same_filesystem(remote, remote)
    different_connection = fsspec.filesystem("memory", namespace="different")
    assert not backends.same_filesystem(first, different_connection)


@pytest.mark.req("STO-14")
@pytest.mark.parametrize("response", [(101, b""), (201, b"\x00" * 40)])
def test_sftp_invalid_extension_response_is_not_unlimited(
    response: tuple[int, bytes],
) -> None:
    from paramiko.message import Message

    fs = SimpleNamespace(
        protocol="sftp",
        ftp=SimpleNamespace(_request=lambda *_args: (response[0], Message(response[1]))),
    )
    with pytest.raises(OSError, match="invalid statvfs"):
        backends.free_space(fs, "/archive")

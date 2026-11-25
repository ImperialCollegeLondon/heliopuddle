"""DEP-21: finding rclone, or downloading the pinned release with a checked SHA256."""

from __future__ import annotations

import hashlib
import io
import threading
import zipfile
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from heliopuddle.storage.rclone import RcloneUnavailable, binary, configure, find_rclone

KEY = "linux-amd64"
NAME = f"rclone-v{binary.RCLONE_VERSION}-{KEY}"


def _archive(content: bytes = b"#!/bin/sh\necho fake\n") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(f"{NAME}/rclone", content)
        zf.writestr(f"{NAME}/README.txt", "readme")
    return buffer.getvalue()


@pytest.fixture
def server() -> Iterator[tuple[str, dict[str, bytes], list[str]]]:
    """A local download site: ``files`` by URL path, ``requests`` seen."""
    files: dict[str, bytes] = {}
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            requests.append(self.path)
            body = files.get(self.path)
            self.send_response(200 if body is not None else 404)
            self.send_header("Content-Length", str(len(body or b"")))
            self.end_headers()
            self.wfile.write(body or b"")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", files, requests
    httpd.shutdown()


@pytest.fixture(autouse=True)
def _reset_settings() -> Iterator[None]:
    yield
    binary.configure_binary(
        binary=None, download_dir=None, auto_download=True, base_url=binary.DEFAULT_BASE_URL
    )


def _serve(files: dict[str, bytes], data: bytes) -> None:
    files[f"/v{binary.RCLONE_VERSION}/{NAME}.zip"] = data


@pytest.mark.req("DEP-21")
def test_download_with_the_right_hash(
    server: tuple[str, dict[str, bytes], list[str]], tmp_path: Path
) -> None:
    url, files, _ = server
    data = _archive()
    _serve(files, data)
    installed = binary.download_rclone(
        tmp_path, base_url=url, key=KEY, sha256={KEY: hashlib.sha256(data).hexdigest()}
    )
    assert installed == tmp_path / f"rclone-v{binary.RCLONE_VERSION}" / "rclone"
    assert installed.read_bytes() == b"#!/bin/sh\necho fake\n"
    assert installed.stat().st_mode & 0o111
    assert sorted(p.name for p in tmp_path.iterdir()) == [f"rclone-v{binary.RCLONE_VERSION}"]


@pytest.mark.req("DEP-21")
def test_download_with_a_wrong_hash_installs_nothing(
    server: tuple[str, dict[str, bytes], list[str]], tmp_path: Path
) -> None:
    url, files, _ = server
    _serve(files, _archive())
    with pytest.raises(RcloneUnavailable, match="SHA256"):
        binary.download_rclone(tmp_path, base_url=url, key=KEY)  # the real pinned hash
    assert list(tmp_path.iterdir()) == []


@pytest.mark.req("DEP-21")
def test_failed_download_and_bad_archive(
    server: tuple[str, dict[str, bytes], list[str]], tmp_path: Path
) -> None:
    url, files, _ = server
    with pytest.raises(RcloneUnavailable, match="downloading rclone"):
        binary.download_rclone(tmp_path, base_url=url, key=KEY)
    bad = b"not a zip"
    _serve(files, bad)
    with pytest.raises(RcloneUnavailable, match="not as expected"):
        binary.download_rclone(
            tmp_path, base_url=url, key=KEY, sha256={KEY: hashlib.sha256(bad).hexdigest()}
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.req("DEP-21")
def test_find_downloads_once_then_reuses(
    server: tuple[str, dict[str, bytes], list[str]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url, files, requests = server
    data = _archive()
    _serve(files, data)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(binary, "platform_key", lambda: KEY)
    monkeypatch.setitem(binary.RCLONE_SHA256, KEY, hashlib.sha256(data).hexdigest())  # type: ignore[arg-type]
    binary.configure_binary(base_url=url, download_dir=tmp_path / "bin")
    first = find_rclone()
    binary.configure_binary(base_url=url)  # forget the cached answer
    assert find_rclone() == first
    assert len(requests) == 1


@pytest.mark.req("DEP-21")
def test_auto_download_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    configure(auto_download=False, download_dir=tmp_path / "bin")
    with pytest.raises(RcloneUnavailable, match="HELIOPUDDLE_RCLONE_AUTO_DOWNLOAD"):
        find_rclone()


@pytest.mark.req("DEP-21")
def test_path_is_preferred_and_configured_binary_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    on_path = tmp_path / "path" / "rclone"
    on_path.parent.mkdir()
    on_path.write_text("#!/bin/sh\n")
    on_path.chmod(0o755)
    monkeypatch.setenv("PATH", str(on_path.parent))
    configure(auto_download=False, download_dir=tmp_path / "bin")
    assert find_rclone() == str(on_path)
    explicit = tmp_path / "explicit-rclone"
    explicit.write_text("#!/bin/sh\n")
    explicit.chmod(0o755)
    configure(binary=str(explicit))
    assert find_rclone() == str(explicit)


@pytest.mark.req("DEP-21")
@pytest.mark.parametrize(
    ("system", "machine", "key"),
    [
        ("Linux", "x86_64", "linux-amd64"),
        ("Linux", "aarch64", "linux-arm64"),
        ("Darwin", "arm64", "osx-arm64"),
        ("Darwin", "x86_64", "osx-amd64"),
        ("Windows", "AMD64", "windows-amd64"),
        ("Windows", "ARM64", "windows-arm64"),
    ],
)
def test_platform_keys(system: str, machine: str, key: str) -> None:
    assert binary.platform_key(system, machine) == key
    assert len(binary.RCLONE_SHA256[key]) == 64


def test_unknown_platform() -> None:
    with pytest.raises(RcloneUnavailable, match="no pinned rclone release"):
        binary.platform_key("Plan9", "mips")


@pytest.mark.req("DEP-21")
def test_docker_image_pins_the_same_release() -> None:
    dockerfile = (Path(__file__).resolve().parents[2] / "docker" / "Dockerfile").read_text()
    assert f"ARG HP_RCLONE_VERSION={binary.RCLONE_VERSION}\n" in dockerfile
    for arch in ("amd64", "arm64"):
        pinned = binary.RCLONE_SHA256[f"linux-{arch}"]
        assert f"ARG HP_RCLONE_SHA256_{arch.upper()}={pinned}\n" in dockerfile
    assert "COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone" in dockerfile

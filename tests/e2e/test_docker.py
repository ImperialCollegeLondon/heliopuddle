"""DEP-01: the Docker image runs everything and a client add from the host works."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from heliopuddle.client import HelioPuddleClient
from heliopuddle.server.schemas import FileState

from .harness import HpContainer, docker


@pytest.mark.req("DEP-01")
def test_health_is_ok(hp_container: HpContainer) -> None:
    r = httpx.get(f"{hp_container.api_url}/health", timeout=10)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok"


@pytest.mark.req("DEP-01")
def test_runs_as_non_root_under_tini(hp_container: HpContainer) -> None:
    assert hp_container.exec("id", "-u") != "0"
    assert hp_container.exec("cat", "/proc/1/comm") == "tini"


@pytest.mark.req("DEP-01")
def test_client_add_from_host(
    hp_container: HpContainer, hp_client: HelioPuddleClient, tmp_path: Path
) -> None:
    content = b"hello from the host\n"
    local = tmp_path / "hello.txt"
    local.write_bytes(content)

    record = hp_client.add_file(local, "e2e/docker/hello.txt")

    assert record.path == "e2e/docker/hello.txt"
    assert record.state == FileState.AVAILABLE
    assert (hp_container.data_dir / "e2e/docker/hello.txt").read_bytes() == content
    assert hp_client.get_file(record.id).state == FileState.AVAILABLE


# Each target protocol of STO-06 resolves to an importable fsspec class in the image.
_RESOLVE_PROTOCOLS = """
import fsspec
for p in ("file", "smb", "sftp", "s3", "az", "abfs", "gs", "gcs"):
    cls = fsspec.get_filesystem_class(p)
    print(p, cls.__module__ + "." + cls.__name__)
"""


@pytest.mark.req("DEP-08")
def test_image_resolves_every_target_protocol(hp_image: str) -> None:
    out = docker("run", "--rm", "--entrypoint", "python", hp_image, "-c", _RESOLVE_PROTOCOLS)
    resolved = dict(line.split(" ", 1) for line in out.splitlines())
    assert sorted(resolved) == sorted(["file", "smb", "sftp", "s3", "az", "abfs", "gs", "gcs"])


@pytest.mark.req("DEP-16")
def test_image_has_no_test_tooling_or_test_files(hp_image: str) -> None:
    """The public image is the e2e image: it must carry nothing used only for testing."""
    import subprocess

    checks = Path(__file__).resolve().parents[2] / "docker" / "image_checks.py"
    result = subprocess.run(
        ["docker", "run", "--rm", "-i", "--entrypoint", "python", hp_image, "-"],
        input=checks.read_text(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "image checks: ok" in result.stdout

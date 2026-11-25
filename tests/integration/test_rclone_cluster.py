"""rclone sessions across processes, and refreshed tokens saved to blocks (STO-20, STO-21).

Runs on Prefect's test harness with the real rclone binary:

- two processes sharing one Prefect never hold an rcd on the same config at
  once, while processes on different configs run together (STO-20);
- a token rclone refreshes during a session (from a fake local token endpoint)
  is saved back to the ``RCloneConfigFileBlock`` (STO-21).
"""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import textwrap
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

import fsspec
import pytest

from heliopuddle.server.services import rclone_hooks
from heliopuddle.storage import rclone
from heliopuddle.storage.rclone import binary

CACHE = Path.home() / ".cache" / "heliopuddle-test" / "bin"
HOLD_S = 2.0

CHILD = textwrap.dedent(
    """
    import json, sys, time
    from heliopuddle.server.services.rclone_hooks import PrefectConfigBlockStore, prefect_lock
    from heliopuddle.storage import rclone

    binary, config, log, tag = sys.argv[1:5]
    rclone.configure(binary=binary, auto_download=False, idle=0.1)
    rclone.set_session_hooks(prefect_lock, PrefectConfigBlockStore())
    with rclone.manager.use([rclone.ConfigSource("file", config)]):
        start = time.time()
        time.sleep(float(sys.argv[5]))
        end = time.time()
    rclone.manager.close(30, force=True)
    with open(log, "a") as f:
        f.write(json.dumps([tag, start, end]) + "\\n")
    """
)


@pytest.fixture(scope="module")
def harness() -> Iterator[str]:
    """A temporary Prefect server; yields its API URL."""
    from prefect.settings import PREFECT_API_URL
    from prefect.testing.utilities import prefect_test_harness

    with prefect_test_harness(server_startup_timeout=180):
        url = PREFECT_API_URL.value()
        assert url
        yield url


@pytest.fixture(scope="module")
def rclone_binary() -> str:
    installed = CACHE / f"rclone-v{binary.RCLONE_VERSION}" / "rclone"
    if not installed.is_file():
        binary.download_rclone(CACHE)
    return str(installed)


def _conf(tmp_path: Path, name: str) -> Path:
    path = tmp_path / f"{name}.conf"
    path.write_text(f"[{name}]\ntype = memory\n")
    return path


def _run_children(
    harness: str, rclone_binary: str, tmp_path: Path, configs: list[Path]
) -> list[tuple[str, float, float]]:
    script = tmp_path / "child.py"
    script.write_text(CHILD)
    log = tmp_path / f"log-{uuid.uuid4().hex[:6]}.jsonl"
    env = {**os.environ, "PREFECT_API_URL": harness}
    procs = [
        subprocess.Popen(
            [sys.executable, str(script), rclone_binary, str(c), str(log), f"p{i}", str(HOLD_S)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        for i, c in enumerate(configs)
    ]
    for p in procs:
        out, _ = p.communicate(timeout=120)
        assert p.returncode == 0, out.decode(errors="replace")
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    return [(str(t), float(s), float(e)) for t, s, e in rows]


def _overlap(a: tuple[str, float, float], b: tuple[str, float, float]) -> bool:
    return a[1] < b[2] and b[1] < a[2]


@pytest.mark.slow
@pytest.mark.req("STO-20")
def test_one_session_per_config_across_processes(
    harness: str, rclone_binary: str, tmp_path: Path
) -> None:
    shared = _conf(tmp_path, "shared")
    first, second = _run_children(harness, rclone_binary, tmp_path, [shared, shared])
    assert not _overlap(first, second), (first, second)

    one, two = _conf(tmp_path, "one"), _conf(tmp_path, "two")
    first, second = _run_children(harness, rclone_binary, tmp_path, [one, two])
    assert _overlap(first, second), (first, second)


class _TokenHandler(http.server.BaseHTTPRequestHandler):
    """A fake OAuth token endpoint: every ``POST /token`` returns a new token."""

    calls: ClassVar[list[str]] = []

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.calls.append(self.path)
        body = json.dumps(
            {
                "access_token": "hp-refreshed-access",
                "refresh_token": "hp-refreshed-refresh",
                "token_type": "Bearer",
                "expires_in": 3600,
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture(scope="module")
def token_endpoint() -> Iterator[str]:
    """A local OAuth token endpoint (rclone runs on this machine); yields its base URL."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _TokenHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()


@pytest.fixture
def hooks(harness: str, rclone_binary: str) -> Iterator[None]:
    rclone.configure(binary=rclone_binary, auto_download=False, idle=0.1)
    rclone.set_session_hooks(rclone_hooks.prefect_lock, rclone_hooks.PrefectConfigBlockStore())
    yield
    rclone.manager.close(30, force=True)
    rclone.set_session_hooks(None, None)
    rclone.configure(binary=None, auto_download=True)


@pytest.mark.slow
@pytest.mark.req("STO-21", "STO-20")
def test_refreshed_token_is_saved_to_the_block(token_endpoint: str, hooks: None) -> None:
    expired = json.dumps(
        {
            "access_token": "hp-old-access",
            "token_type": "Bearer",
            "refresh_token": "hp-old-refresh",
            "expiry": "2000-01-01T00:00:00Z",
        }
    )
    config = textwrap.dedent(
        f"""\
        [od]
        type = onedrive
        client_id = hp-test-client
        client_secret = hp-test-secret
        auth_url = {token_endpoint}/authorize
        token_url = {token_endpoint}/token
        token = {expired}
        drive_id = hp-test-drive
        drive_type = business
        """
    )
    name = f"rclone-{uuid.uuid4().hex[:8]}"
    block_cls = rclone_hooks.config_block_class()
    block_cls(remote_name="od", config_file_contents=config).save(name, _sync=True)

    fs = fsspec.filesystem("rclone", config_block=name, skip_instance_cache=True)
    # The refresh succeeds (WireMock); the Graph call after it fails: there is no
    # real drive. The new token must be saved all the same.
    with pytest.raises(Exception):  # noqa: B017 - any failure of the backend call
        fs.ls("od/hp")
    rclone.manager.close(30, force=True)

    assert "/token" in _TokenHandler.calls
    saved = rclone_hooks.PrefectConfigBlockStore().load(name)
    assert "hp-refreshed-access" in saved
    assert "hp-old-access" not in saved

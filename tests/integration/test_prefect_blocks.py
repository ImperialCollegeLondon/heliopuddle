"""Storage locations resolved from real Prefect blocks (STO-18, STO-19).

Runs on Prefect's test harness (an in-process temporary Prefect server): a
storage location whose blocks are missing is incomplete; once the blocks are
saved it works after the cache expires, without a restart.
"""

from __future__ import annotations

import io
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from heliopuddle.client import HelioPuddleClient
from heliopuddle.config import load_project
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services.blocks import PrefectBlockResolver, install_block_resolver
from heliopuddle.storage import credentials as creds

from .conftest import HpProcess, base_env, free_port, start_hp


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(scope="module")
def harness() -> Iterator[None]:
    from prefect.testing.utilities import prefect_test_harness

    with prefect_test_harness(server_startup_timeout=180):
        yield


@pytest.fixture
def clock(harness: None) -> Iterator[Clock]:
    c = Clock()
    install_block_resolver()
    creds.set_clock(c)
    yield c
    creds.set_clock(None)
    creds.set_block_resolver(None)


@pytest.mark.req("STO-18", "STO-19")
def test_missing_blocks_then_saved_work_without_restart(tmp_path: Path, clock: Clock) -> None:
    from prefect.blocks.system import Secret
    from prefect.filesystems import RemoteFileSystem

    suffix = uuid.uuid4().hex[:8]
    secret, fs_block, opts = f"root-{suffix}", f"fs-{suffix}", f"opts-{suffix}"
    path = tmp_path / "p.yaml"
    path.write_text(
        "project: p\nstorage_locations:\n"
        f"  - name: vault\n    url: 'file://{{{{ prefect.blocks.secret.{secret} }}}}'\n"
        f"    storage_options: '{{{{ prefect.blocks.secret.{opts} }}}}'\n"
        f"  - name: offsite\n    block: {fs_block}\n"
        "    storage_options:\n      auto_mkdir: true\n"
    )
    vault_cfg, offsite_cfg = load_project(path).storage_locations
    assert isinstance(creds.get_block_resolver(), PrefectBlockResolver)

    problems = creds.location_problems(vault_cfg)
    assert f"{path}: storage_locations.0.url: Prefect Secret block {secret!r} not found" in problems
    assert any(f"Prefect Secret block {opts!r} not found" in p for p in problems)
    assert creds.location_problems(offsite_cfg) == [
        f"{path}: storage_locations.1.block: Prefect RemoteFileSystem block {fs_block!r} not found"
    ]
    with pytest.raises(creds.StorageLocationIncomplete):
        creds.open_location(vault_cfg)

    vault_root = tmp_path / "vault"
    Secret(value=str(vault_root)).save(secret)
    Secret(value='{"auto_mkdir": true}').save(opts)
    offsite_url = f"memory://offsite-{suffix}/root"  # a block's basepath needs a location
    RemoteFileSystem(basepath=offsite_url, settings={}).save(fs_block)
    assert creds.location_problems(vault_cfg)  # still cached
    clock.now += creds.CACHE_TTL + 1

    assert creds.location_problems(vault_cfg) == []
    assert creds.resolve(vault_cfg) == (f"file://{vault_root}", {"auto_mkdir": True})
    assert creds.resolve(offsite_cfg) == (offsite_url, {"auto_mkdir": True})
    for cfg in (vault_cfg, offsite_cfg):
        store = creds.open_location(cfg)
        store.ensure_root()
        store.write("d/a.txt", io.BytesIO(b"from blocks"))
        with store.open_read("d/a.txt") as f:
            assert f.read() == b"from blocks"
    assert (vault_root / "d" / "a.txt").read_bytes() == b"from blocks"


@pytest.mark.req("STO-18")
@pytest.mark.slow
def test_server_starts_with_a_missing_block_and_picks_it_up(
    run_hp: list[HpProcess], tmp_path: Path
) -> None:
    from prefect.blocks.system import Secret
    from prefect.settings import PREFECT_API_URL, temporary_settings

    config_dir, data_dir, vault = tmp_path / "config", tmp_path / "data", tmp_path / "vault"
    config_dir.mkdir()
    (config_dir / "p.yaml").write_text(
        "project: p\nstorage_locations:\n"
        f"  - name: primary\n    url: file://{tmp_path / 'primary'}\n"
        "  - name: vault\n    url: 'file://{{ prefect.blocks.secret.vault-root }}'\n"
    )
    port, prefect_port = free_port(), free_port()
    env = base_env(
        HELIOPUDDLE_CONFIG_DIR=str(config_dir),
        HELIOPUDDLE_DATA_DIR=str(data_dir),
        PREFECT_SERVER_API_PORT=str(prefect_port),
        PREFECT_HOME=str(data_dir / "parent-prefect"),
    )
    proc = start_hp(["server", "start", "--port", str(port)], env, tmp_path / "server.log")
    run_hp.append(proc)
    api = f"http://127.0.0.1:{port}/api/v1"
    proc.wait_for(f"{api}/health")
    assert "'vault' of project 'p' is incomplete" in proc.log()
    assert "Prefect Secret block 'vault-root' not found" in proc.log()

    def vault_row() -> dict[str, object]:
        rows = httpx.get(f"{api}/projects/p/storage-locations").json()
        return next(r for r in rows if r["name"] == "vault")

    row = vault_row()
    assert row["status"] == "incomplete"
    assert row["problems"] == [
        f"{config_dir / 'p.yaml'}: storage_locations.1.url: "
        "Prefect Secret block 'vault-root' not found"
    ]

    with temporary_settings({PREFECT_API_URL: f"http://127.0.0.1:{prefect_port}/api"}):
        Secret(value=str(vault)).save("vault-root")
    deadline = time.monotonic() + creds.CACHE_TTL + 30
    while vault_row()["status"] != "ok":  # the 60s cache expires: no restart
        assert time.monotonic() < deadline, vault_row()
        time.sleep(2)
    local = tmp_path / "a.txt"
    local.write_bytes(b"vault")
    with HelioPuddleClient(api, "p", user="itest") as client:
        record = client.add_file(local, "a.txt", storage_location="vault")
    assert record.state == FileState.AVAILABLE
    assert (vault / "a.txt").read_bytes() == b"vault"

"""DEP-10: ``heliopuddle server start`` with no project config writes and loads a default file."""

from __future__ import annotations

from pathlib import Path

import pytest

from heliopuddle.client import HelioPuddleClient
from heliopuddle.server.schemas import FileState

from .conftest import HpProcess, base_env, free_port, start_hp


@pytest.mark.req("DEP-10")
@pytest.mark.req("STO-08")
@pytest.mark.slow
def test_start_without_config_serves_the_default_project(
    run_hp: list[HpProcess], tmp_path: Path
) -> None:
    config_dir, data_dir = tmp_path / "config", tmp_path / "data"
    config_dir.mkdir()  # empty: no project files
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
    store = (data_dir / "primary-storage").resolve()

    assert store.is_dir()  # created at startup
    assert (data_dir / "default_config.yaml").is_file()
    assert "using the default config file" in proc.log()
    local = tmp_path / "report_v001.csv"
    local.write_bytes(b"a,b\n1,2\n")
    with HelioPuddleClient(api, "default", user="itest") as client:
        locations = client.list_storage_locations()
        record = client.add_file(local, "reports/report_v001.csv")
        listed = [f.path for f in client.list_files().items]

    archive = (data_dir / "archive-storage").resolve()
    assert [(sl.name, sl.url) for sl in locations] == [
        ("primary", store.as_uri()),
        ("archive", archive.as_uri()),
    ]
    assert record.state == FileState.AVAILABLE
    assert listed == ["reports/report_v001.csv"]
    assert (store / "reports" / "report_v001.csv").read_bytes() == b"a,b\n1,2\n"

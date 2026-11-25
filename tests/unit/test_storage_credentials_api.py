"""Incomplete storage locations in the API and UI (STO-18, STO-19)."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from heliopuddle.server.api import create_api
from heliopuddle.server.context import AppContext
from heliopuddle.storage import credentials as creds
from tests.unit._ui_helpers import Factory, sl_read, ui_client
from tests.unit._ui_pages_helpers import fleet
from tests.unit.test_storage_credentials import Clock, FakeResolver

VAULT_YAML = """\
project: demo
storage_locations:
  - name: primary
    url: file://{store}
  - name: vault
    url: "file://{{{{ prefect.blocks.secret.vault-root }}}}"
  - name: offsite
    block: offsite-fs
    storage_options:
      auto_mkdir: true
"""


@pytest.fixture
def config_dir(tmp_path: Path, store: Path) -> Path:
    path = tmp_path / "config"
    path.mkdir()
    (path / "demo.yaml").write_text(VAULT_YAML.format(store=store))
    return path


@pytest.fixture
def clock() -> Iterator[Clock]:
    c = Clock()
    creds.set_clock(c)
    yield c
    creds.set_clock(None)


@pytest.fixture
def blocks(clock: Clock) -> Iterator[FakeResolver]:
    resolver = FakeResolver()
    creds.set_block_resolver(resolver)
    yield resolver
    creds.set_block_resolver(None)


@pytest.fixture
def client(ctx: AppContext, blocks: FakeResolver) -> Iterator[TestClient]:
    with TestClient(create_api(ctx)) as c:
        yield c


@pytest.mark.req("STO-18", "STO-19")
def test_listing_reports_incomplete_locations(client: TestClient, config_dir: Path) -> None:
    rows = {r["name"]: r for r in client.get("/projects/demo/storage-locations").json()}
    assert rows["primary"]["status"] == "ok"
    assert rows["primary"]["problems"] == []
    assert rows["vault"]["status"] == "incomplete"
    assert rows["vault"]["url"] == "file://{{ prefect.blocks.secret.vault-root }}"
    assert rows["vault"]["problems"] == [
        f"{config_dir / 'demo.yaml'}: storage_locations.1.url: "
        "Prefect Secret block 'vault-root' not found"
    ]
    assert rows["offsite"]["status"] == "incomplete"
    assert rows["offsite"]["url"] == "prefect-block:offsite-fs"
    assert "RemoteFileSystem block 'offsite-fs' not found" in rows["offsite"]["problems"][0]


@pytest.mark.req("STO-18", "STO-19")
def test_detail_resolves_once_blocks_exist(
    client: TestClient, blocks: FakeResolver, clock: Clock, tmp_path: Path
) -> None:
    detail = client.get("/projects/demo/storage-locations/offsite").json()
    assert detail["status"] == "incomplete"
    assert detail["url"] == "prefect-block:offsite-fs"
    assert detail["storage_options"] == {"auto_mkdir": True}  # unresolved

    blocks.secrets["offsite-key"] = "Api-Test-Secret-Key-81"
    blocks.filesystems["offsite-fs"] = (
        f"file://{tmp_path / 'offsite'}",
        {"key": "{{ prefect.blocks.secret.offsite-key }}", "auto_mkdir": False},
    )
    clock.now += creds.CACHE_TTL + 1  # no restart: the cache expires
    detail = client.get("/projects/demo/storage-locations/offsite").json()
    assert detail["status"] == "ok"
    assert detail["problems"] == []
    assert detail["url"] == f"file://{tmp_path / 'offsite'}"
    assert detail["storage_options"] == {"key": "Api-Test-Secret-Key-81", "auto_mkdir": True}


def _put(root: Path, path: str, data: bytes) -> dict[str, object]:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return {"path": path, "hash": hashlib.md5(data).hexdigest(), "size": len(data)}


@pytest.mark.req("STO-18")
def test_api_call_needing_an_incomplete_location_returns_409(
    client: TestClient, blocks: FakeResolver, clock: Clock, tmp_path: Path
) -> None:
    vault = tmp_path / "vault"
    blocks.secrets["vault-root"] = str(vault)
    body = _put(vault, "a.dat", b"vault data") | {"storage_location": "vault"}
    created = client.post("/projects/demo/files", json=body)
    assert created.status_code == 201, created.text
    file_id = created.json()["id"]
    assert client.get(f"/files/{file_id}/content").content == b"vault data"

    del blocks.secrets["vault-root"]
    clock.now += creds.CACHE_TTL + 1
    for url in (f"/files/{file_id}/content", f"/files/{file_id}/preview"):
        r = client.get(url)
        assert r.status_code == 409, r.text
        error = r.json()
        assert error["reason"] == "storage_location_incomplete"
        assert error["problems"] == [
            f"{tmp_path / 'config' / 'demo.yaml'}: storage_locations.1.url: "
            "Prefect Secret block 'vault-root' not found"
        ]
        assert "vault-root" in error["detail"]


# --- UI ---------------------------------------------------------------------------

PROBLEM = "demo.yaml: storage_locations.1.url: Prefect Secret block 'vault-root' not found"


def _incomplete_factory() -> Factory:
    return Factory(
        files=fleet(),
        storage_locations=[
            sl_read("ingest"),
            sl_read("vault", status="incomplete", problems=[PROBLEM]),
        ],
    )


@pytest.mark.req("STO-18")
def test_storage_list_warns_about_incomplete_locations() -> None:
    client = ui_client(_incomplete_factory(), prefect_ui_url="http://prefect.example:4200")
    html = client.get("/projects/demo/storage").text
    assert html.count('class="alert sl-incomplete"') == 1
    banner = html.split('class="alert sl-incomplete"')[1].split("</div>")[0]
    assert "Storage location vault is incomplete" in banner
    assert "Prefect Secret block &#39;vault-root&#39; not found" in banner
    assert 'href="http://prefect.example:4200/blocks"' in banner
    cards = html.split('<article class="card sl-card')[1:]
    assert ">incomplete</span>" in cards[1]
    assert ">incomplete</span>" not in cards[0]


@pytest.mark.req("STO-18")
def test_storage_location_page_warns_without_javascript() -> None:
    html = ui_client(_incomplete_factory()).get("/projects/demo/storage/vault").text
    banner = html.split('class="alert sl-incomplete"')[1].split("</div>")[0]
    assert "<li" in banner and "vault-root" in banner
    assert "/blocks" not in banner  # no Prefect UI URL known: no link
    ok = ui_client(_incomplete_factory()).get("/projects/demo/storage/ingest").text
    assert "sl-incomplete" not in ok

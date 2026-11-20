"""API-only content transfers against the built image and its real storage."""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from heliopuddle.client import HelioPuddleClient, PermissionDeniedError
from heliopuddle.storage.location import StorageLocation

from .conftest import AzureHp, LocalAuthHp


@pytest.mark.req("AUTH-07", "AUTH-08", "AUTH-15", "CLI-01", "CLI-06", "CLI-08")
@pytest.mark.slow
def test_scoped_clients_use_api_without_storage_credentials(
    hp_local_auth: LocalAuthHp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hp = hp_local_auth.hp
    with hp_local_auth.client() as admin:
        admin.create_user("content-writer", "writer-password-1", roles=[f"hp-writer:{hp.project}"])
        admin.create_user("content-reader", "reader-password-1", roles=[f"hp-reader:{hp.project}"])

    def no_direct_access(*args: object, **kwargs: object) -> None:
        pytest.fail("ordinary readers/writers must not access the backing store directly")

    monkeypatch.setattr(StorageLocation, "__init__", no_direct_access)
    source = tmp_path / "source.dat"
    source.write_bytes(b"API-only content\n" * 1000)
    with HelioPuddleClient(
        hp.api_url, hp.project, username="content-writer", password="writer-password-1"
    ) as writer:
        record = writer.add_file(source, "api-content/file.dat")
        assert record.state == "available"
        assert writer.add_file(source, record.path).id == record.id
        source.write_bytes(b"replacement through the API")
        replaced = writer.add_file(source, record.path, overwrite=True)
        assert replaced.id == record.id
    with HelioPuddleClient(
        hp.api_url, hp.project, username="content-reader", password="reader-password-1"
    ) as reader:
        assert reader.list_storage_locations()[0].url == ""
        with pytest.raises(PermissionDeniedError):
            reader.get_storage_location()
        with reader.open(record.id) as content:
            assert content.read() == source.read_bytes()
        assert (
            reader.download(record.id, tmp_path / "download.dat").read_bytes()
            == source.read_bytes()
        )
        with pytest.raises(PermissionDeniedError):
            reader.add_file(source, "api-content/forbidden.dat")


@pytest.mark.req("AUTH-15", "API-03", "STO-02", "STO-09", "STO-10")
@pytest.mark.slow
def test_api_content_transfers_on_object_storage(hp_azure: AzureHp) -> None:
    data = b"streamed through the API to object storage"
    hp = hp_azure.hp
    with httpx.Client(base_url=hp.api_url, timeout=120) as api:
        reserved = api.post(
            f"/projects/{hp.project}/uploads",
            json={
                "path": "e2e/content/object.dat",
                "hash": hashlib.md5(data, usedforsecurity=False).hexdigest(),
                "size": len(data),
            },
        )
        reserved.raise_for_status()
        token = reserved.json()["token"]
        written = api.put(f"/uploads/{token}/content", content=data)
        assert written.status_code == 204, written.text
        done = api.post(f"/uploads/{token}/complete")
        done.raise_for_status()
        assert done.json()["state"] == "available"
        content = api.get(f"/files/{done.json()['id']}/content")
        assert content.status_code == 200
        assert content.content == data

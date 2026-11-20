"""STO-07 conformance suite on Azure Blob (Azurite), the canonical cloud backend (STO-02).

Also the Azure facts that :mod:`heliopuddle.storage.backends` relies on: which
uploads carry a service-computed md5 and how it is reported.
"""

from __future__ import annotations

import hashlib
import io
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from adlfs import AzureBlobFileSystem

from heliopuddle.storage import StorageLocation, StorageRootMissingError
from heliopuddle.storage.location import CreateRoot
from tests.storage_conformance import LocationFactory, MissingRootFactory, StorageConformance

pytestmark = [pytest.mark.req("STO-07"), pytest.mark.req("STO-02")]


def _fs(conn: str) -> AzureBlobFileSystem:
    return AzureBlobFileSystem(connection_string=conn, skip_instance_cache=True)


def _container_name() -> str:
    return f"hp-{uuid.uuid4().hex[:12]}"


class TestAzuriteConformance(StorageConformance):
    object_store = True

    @pytest.fixture
    def location_factory(self, azurite: str) -> LocationFactory:
        def make() -> StorageLocation:
            loc = StorageLocation(
                f"az://{_container_name()}/root/dir",
                {"connection_string": azurite},
                create_root=True,
            )
            loc.ensure_root()  # a fresh container
            return loc

        return make

    @pytest.fixture
    def missing_root_factory(self, azurite: str) -> MissingRootFactory:
        def make(create_root: CreateRoot) -> tuple[StorageLocation, Callable[[], bool]]:
            container = f"hp-missing-{uuid.uuid4().hex[:12]}"
            loc = StorageLocation(
                f"az://{container}/root", {"connection_string": azurite}, create_root
            )
            return loc, lambda: bool(_fs(azurite).exists(container))

        return make


@pytest.fixture
def loc(azurite: str) -> StorageLocation:
    """A storage location on a container that does not exist yet (``create_root: true``)."""
    return StorageLocation(
        f"az://{_container_name()}/root/dir", {"connection_string": azurite}, create_root=True
    )


@pytest.mark.req("STO-08")
def test_auto_does_not_create_a_container(azurite: str) -> None:
    container = _container_name()
    loc = StorageLocation(f"az://{container}/root", {"connection_string": azurite})

    with pytest.raises(StorageRootMissingError, match=container):
        loc.ensure_root()
    with pytest.raises(StorageRootMissingError):
        loc.write("f.txt", io.BytesIO(b"f"))

    assert not _fs(azurite).exists(container)


@pytest.mark.req("STO-08")
def test_true_creates_a_container(azurite: str) -> None:
    container = _container_name()
    loc = StorageLocation(
        f"az://{container}/root", {"connection_string": azurite}, create_root=True
    )

    loc.write("f.txt", io.BytesIO(b"f"))

    assert _fs(azurite).exists(container)
    assert [e.path for e in loc.list_files()] == ["f.txt"]
    loc.ensure_root()  # idempotent once it exists


def test_streamed_write_has_no_native_md5(loc: StorageLocation) -> None:
    # adlfs uploads fs.open(..., "wb") as staged blocks; Azure sets no
    # Content-MD5 for a block list, so the indexer reads the blob.
    loc.write("streamed.bin", io.BytesIO(b"streamed"))

    assert loc.native_md5("streamed.bin") is None
    assert [e.md5 for e in loc.list_files()] == [None]


def test_single_put_blob_has_native_md5(loc: StorageLocation) -> None:
    # A single Put Blob (az CLI, Storage Explorer, adlfs pipe/put_file for small
    # files) gets a service-computed Content-MD5, reported as 16 raw bytes.
    data = b"single put blob\n" * 100
    loc.ensure_root()
    loc.fs.pipe(f"{loc.root}/put.txt", data)

    expected = hashlib.md5(data).hexdigest()
    assert loc.native_md5("put.txt") == expected
    assert {e.path: e.md5 for e in loc.list_files()} == {"put.txt": expected}


def test_forged_content_md5_is_reported_but_md5_reads(azurite: str, loc: StorageLocation) -> None:
    from azure.storage.blob import BlobServiceClient, ContentSettings

    loc.write("lie.txt", io.BytesIO(b"real content"))  # creates the container
    container, blob_path = loc.root.split("/", 1)
    blob = BlobServiceClient.from_connection_string(azurite).get_blob_client(
        container, f"{blob_path}/lie.txt"
    )
    blob.set_http_headers(ContentSettings(content_md5=bytearray(hashlib.md5(b"other").digest())))

    assert loc.native_md5("lie.txt") == hashlib.md5(b"other").hexdigest()
    assert loc.md5("lie.txt") == hashlib.md5(b"real content").hexdigest()


def test_list_files_skips_directory_markers_and_outside_blobs(loc: StorageLocation) -> None:
    container = loc.root.split("/", 1)[0]
    loc.write("a/1.txt", io.BytesIO(b"1"))  # creates the container
    loc.fs.pipe(f"{container}/outside.txt", b"not under the root")
    loc.fs.makedirs(f"{loc.root}/empty/dir", exist_ok=True)

    assert [e.path for e in loc.list_files()] == ["a/1.txt"]


def test_storage_options_from_config_env_var(
    azurite: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from heliopuddle.config import load_projects

    container = _container_name()
    monkeypatch.setenv("HP_TEST_AZURE_CONN", azurite)
    (tmp_path / "az.yaml").write_text(
        "project: az\n"
        "storage_locations:\n"
        "  - name: blob\n"
        f"    url: az://{container}/data\n"
        "    create_root: true\n"
        "    storage_options:\n"
        "      connection_string: ${HP_TEST_AZURE_CONN}\n"
    )
    cfg = load_projects(tmp_path)["az"].storage_location("blob")

    loc = StorageLocation(cfg.url, cfg.storage_options, cfg.create_root)
    loc.write("f.txt", io.BytesIO(b"from config"))

    assert loc.md5("f.txt") == hashlib.md5(b"from config").hexdigest()
    assert [e.path for e in loc.list_files()] == ["f.txt"]

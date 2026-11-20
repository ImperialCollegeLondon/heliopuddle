"""Files API version overrides."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.unit import _files_api_helpers as _fixtures
from tests.unit._files_api_helpers import _add

client = _fixtures.client
config_dir = _fixtures.config_dir
no_verify = _fixtures.no_verify


ADD_URLS = ("/projects/demo/files", "/projects/demo/uploads")


def _post(client: TestClient, url: str, path: str, data: bytes = b"x", **extra: Any) -> Any:
    body = {"path": path, "hash": hashlib.md5(data).hexdigest(), "size": len(data)} | extra
    return client.post(url, json=body)


def _record(r: Any) -> dict[str, Any]:
    """The file record of a register (``201``) or reserve (``200``) answer."""
    assert r.status_code in (200, 201), r.text
    body = r.json()
    return body["file"] if "mode" in body else body


@pytest.mark.req("API-20")
@pytest.mark.usefixtures("no_verify")
@pytest.mark.parametrize("url", ADD_URLS)
def test_version_override_on_register_and_reserve(client: TestClient, url: str) -> None:
    number = _record(_post(client, url, "o/a_v3.cdf", version_number=12))
    assert (number["version_number"], number["version_text"]) == (12, None)
    assert number["series_key"] == "o/a_v{v}.cdf"  # the series still comes from the path
    text = _record(_post(client, url, "o/notes.txt", version_text="001.0002"))
    assert (text["version_number"], text["version_text"]) == (None, "001.0002")
    assert text["series_key"] == "o/notes.txt"
    largest = _record(_post(client, url, "o/big.txt", version_number=2**63 - 1))
    assert largest["version_number"] == 2**63 - 1
    # No override: the rules apply; no rule: unversioned.
    derived = _record(_post(client, url, "o/b_v4.cdf"))
    assert (derived["version_number"], derived["version_text"]) == (4, None)
    plain = _record(_post(client, url, "o/plain.txt"))
    assert (plain["version_number"], plain["version_text"]) == (None, None)
    for bad in (
        {"version_number": 1, "version_text": "1"},
        {"version_number": -1},
        {"version_number": 2**63},
        {"version_text": ""},
        {"version_text": "x" * 256},
    ):
        r = _post(client, url, "o/bad_v1.cdf", **bad)
        assert r.status_code == 422, (bad, r.text)
    both = _post(client, url, "o/bad_v1.cdf", version_number=1, version_text="1")
    assert "not both" in both.text
    assert (
        client.get("/projects/demo/files/by-path", params={"path": "o/bad_v1.cdf"}).status_code
        == 404
    )


@pytest.mark.req("API-21")
@pytest.mark.usefixtures("no_verify")
@pytest.mark.parametrize("url", ADD_URLS)
def test_version_of_the_other_kind_is_422(client: TestClient, url: str) -> None:
    first = _record(_post(client, url, "cal/x_v1.2.json"))
    assert first["version_text"] == "001.0002"
    r = _post(client, url, "cal/x_v1.3.json", version_number=5)
    assert r.status_code == 422, r.text
    (error,) = r.json()["detail"]
    assert "holds text versions" in error["msg"]
    assert error["ctx"] == {"series_version_type": "text", "version_type": "number"}
    _record(_post(client, url, "k/a_v1.cdf"))
    r = _post(client, url, "k/a_v2.cdf", version_text="2")
    assert r.status_code == 422
    assert "holds number versions" in r.json()["detail"][0]["msg"]
    versions = client.get(f"/series/{first['series_id']}/versions").json()
    assert versions["version_type"] == "text"
    assert len(versions["items"]) == 1


@pytest.mark.req("API-22")
def test_version_is_immutable(client: TestClient, store: Path) -> None:
    first = _add(client, store, "i/a_v3.cdf", b"one")
    url = "/projects/demo/files"
    # No override, or an equal one, keeps the version.
    assert _post(client, url, "i/a_v3.cdf", b"one").status_code == 200
    assert _post(client, url, "i/a_v3.cdf", b"one", version_number=3).status_code == 200
    r = _post(client, url, "i/a_v3.cdf", b"one", version_number=4)
    assert r.status_code == 409, r.text
    body = r.json()
    assert "a version is set only when the file is created" in body["detail"]
    assert body["file"]["id"] == first["id"]
    assert "number 3" in body["detail"]
    # Re-reserving and overwriting with a different override are refused too.
    uploads = "/projects/demo/uploads"
    assert _post(client, uploads, "i/a_v3.cdf", b"one", version_number=3).json()["mode"] == (
        "unchanged"
    )
    r = _post(client, uploads, "i/a_v3.cdf", b"one", version_text="3")
    assert (r.status_code, r.json()["file"]["id"]) == (409, first["id"])
    assert "number 3" in r.json()["detail"]
    r = _post(client, uploads, "i/a_v3.cdf", b"two", overwrite=True, version_number=9)
    assert (r.status_code, r.json()["file"]["id"]) == (409, first["id"])
    assert "number 3" in r.json()["detail"]
    # A hash conflict still says so.
    r = _post(client, url, "i/a_v3.cdf", b"three")
    assert r.status_code == 409
    assert "a different file already exists" in r.json()["detail"]
    ok = _post(client, uploads, "i/a_v3.cdf", b"two", overwrite=True, version_number=3)
    assert ok.json()["mode"] == "overwrite"
    # Metadata and content-date updates never change the version.
    assert client.patch(f"/files/{first['id']}/metadata", json={"a": 1}).status_code == 200
    r = client.patch(
        f"/files/{first['id']}/content-date", json={"content_date": "2024-01-01T00:00:00Z"}
    )
    assert r.status_code == 200
    rec = client.get(f"/files/{first['id']}").json()
    assert (rec["version_number"], rec["version_text"]) == (3, None)

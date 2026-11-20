"""Files API registering, re-registering and conflicts."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from heliopuddle.server import models
from heliopuddle.server.api.files import register as register_api
from heliopuddle.server.schemas import FileState
from tests.unit import _files_api_helpers as _fixtures
from tests.unit._files_api_helpers import _add, _put

client = _fixtures.client
config_dir = _fixtures.config_dir
no_verify = _fixtures.no_verify


@pytest.mark.req("API-02")
@pytest.mark.usefixtures("no_verify")
def test_register_uses_project_version_rules(client: TestClient, store: Path) -> None:
    v14 = _add(client, store, "cal/x_v1.4.json")
    v21 = _add(client, store, "cal/x_v2.1.json")
    assert v14["series_key"] == "cal/x_v{major}.{minor}.json"
    assert (v14["version_number"], v14["version_text"]) == (None, "001.0004")
    assert v14["series_id"] == v21["series_id"]
    # text versions compare as strings: "002.0001" beats "001.0004"
    assert client.get(f"/files/{v14['id']}").json()["is_latest"] is False
    assert v21["is_latest"] is True
    versions = client.get(f"/series/{v14['series_id']}/versions").json()
    assert versions["version_type"] == "text"
    assert [f["version_text"] for f in versions["items"]] == ["002.0001", "001.0004"]

    dated = _add(client, store, "sci/a_20250304_v2.cdf")
    assert (dated["version_number"], dated["version_text"]) == (2, None)
    assert dated["content_date"] == "2025-03-04T00:00:00Z"
    given = _add(client, store, "sci/b_20250304_v1.cdf", content_date="2024-01-02T03:04:05Z")
    assert given["content_date"] == "2024-01-02T03:04:05Z"
    plain = _add(client, store, "other/c_v7.cdf")
    assert (plain["version_number"], plain["series_key"]) == (7, "other/c_v{v}.cdf")


@pytest.mark.req("API-07")
def test_reregister_same_hash_is_200_and_verifies_only_when_needed(
    client: TestClient, store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = _put(store, "d/a_v1.dat", b"one")
    first = client.post("/projects/demo/files", json=body)
    assert first.status_code == 201
    assert first.json()["state"] == "available"

    calls: list[int] = []
    real = register_api.verify_file.fn
    monkeypatch.setattr(register_api.verify_file, "fn", lambda i: calls.append(i) or real(i))
    again = client.post("/projects/demo/files", json=body | {"hash": body["hash"].upper()})
    assert again.status_code == 200
    assert again.json()["id"] == first.json()["id"]
    assert calls == []

    with client.app.state.ctx.write_session() as session:  # type: ignore[attr-defined]
        copy_id = models.get_file(session, first.json()["id"]).copies[0].id  # type: ignore[union-attr]
        models.set_copy_state(session, copy_id, FileState.UNKNOWN)
    healed = client.post("/projects/demo/files", json=body)
    assert healed.status_code == 200
    assert healed.json()["state"] == "available"
    assert calls == [copy_id]


@pytest.mark.req("API-07")
def test_conflict_409_then_overwrite(client: TestClient, store: Path) -> None:
    first = client.post(
        "/projects/demo/files",
        json=_put(store, "d/b_v1.dat", b"old", metadata={"k": 1}),
        headers={"X-Forwarded-User": "alice"},
    ).json()

    new = _put(store, "d/b_v1.dat", b"new content")
    r = client.post("/projects/demo/files", json=new, headers={"X-Forwarded-User": "bob"})
    assert r.status_code == 409
    body = r.json()
    assert "already exists" in body["detail"]
    assert body["file"]["id"] == first["id"]
    assert body["file"]["hash"] == first["hash"]

    # A register never overwrites: 409 pointing at the upload protocol (STO-09).
    r = client.post(
        "/projects/demo/files", json=new | {"overwrite": True}, headers={"X-Forwarded-User": "bob"}
    )
    assert r.status_code == 409
    assert "/projects/demo/uploads" in r.json()["detail"]
    assert (store / "d/b_v1.dat").read_bytes() == b"new content"  # written by the test itself
    (store / "d/b_v1.dat").write_bytes(b"old")

    res = client.post(
        "/projects/demo/uploads",
        json={"path": "d/b_v1.dat", "hash": new["hash"], "size": new["size"], "overwrite": True},
        headers={"X-Forwarded-User": "bob"},
    ).json()
    assert res["mode"] == "overwrite"
    (store / res["write_path"]).write_bytes(b"new content")
    r = client.post(f"/uploads/{res['token']}/complete", headers={"X-Forwarded-User": "bob"})
    assert r.status_code == 200, r.text
    rec = r.json()
    assert rec["id"] == first["id"]
    assert rec["hash"] == new["hash"]
    assert rec["size"] == len(b"new content")
    assert rec["state"] == "available"
    assert (rec["created_by"], rec["updated_by"]) == ("alice", "bob")
    assert rec["version_number"] == 1
    assert rec["metadata"] == {"k": 1}
    assert rec["is_latest"] is True
    assert (store / "d/b_v1.dat").read_bytes() == b"new content"


@pytest.mark.req("API-07")
def test_check_and_by_path(client: TestClient, store: Path, no_verify: list[int]) -> None:
    rec = _add(client, store, "d/c.dat", b"c")
    digest = rec["hash"]

    def check(path: str, hash: str) -> dict:
        r = client.post("/projects/demo/files/check", json={"path": path, "hash": hash})
        assert r.status_code == 200, r.text
        return r.json()

    assert check("d/c.dat", digest.upper()) | {"file": None} == {
        "exists": True,
        "hash_matches": True,
        "file": None,
    }
    assert check("d/c.dat", digest)["file"]["id"] == rec["id"]
    mismatch = check("d/c.dat", "0" * 32)
    assert (mismatch["exists"], mismatch["hash_matches"]) == (True, False)
    assert check("d/none.dat", digest) == {"exists": False, "hash_matches": False, "file": None}
    assert (
        client.post("/projects/demo/files/check", json={"path": "../x", "hash": "h"}).status_code
        == 422
    )
    assert (
        client.post("/projects/nope/files/check", json={"path": "x", "hash": "h"}).status_code
        == 404
    )

    got = client.get("/projects/demo/files/by-path", params={"path": "d/c.dat"})
    assert got.status_code == 200
    assert got.json()["id"] == rec["id"]
    assert client.get("/projects/demo/files/by-path", params={"path": "d/x"}).status_code == 404
    bad = client.get("/projects/demo/files/by-path", params={"path": "/abs"})
    assert bad.status_code == 422
    assert bad.json()["detail"][0]["loc"] == ["query", "path"]
    assert client.get("/projects/nope/files/by-path", params={"path": "x"}).status_code == 404

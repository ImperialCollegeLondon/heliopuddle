"""Files API deletion and metadata patches."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from heliopuddle.server import models
from heliopuddle.server.api.files import delete as delete_api
from heliopuddle.server.database.orm import File, utcnow
from heliopuddle.storage import StorageLocation
from tests.unit import _files_api_helpers as _fixtures
from tests.unit._files_api_helpers import _add, _put

client = _fixtures.client
config_dir = _fixtures.config_dir
no_verify = _fixtures.no_verify


@pytest.mark.req("API-07")
def test_deleted_path_is_re_added_as_a_new_row(client: TestClient, store: Path) -> None:
    first = _add(client, store, "d/e.dat", b"e")
    assert client.delete(f"/files/{first['id']}").status_code == 200
    assert client.get("/projects/demo/files/by-path", params={"path": "d/e.dat"}).status_code == 404
    check = client.post(
        "/projects/demo/files/check", json={"path": "d/e.dat", "hash": first["hash"]}
    ).json()
    assert check["exists"] is False
    r = client.post("/projects/demo/files", json=_put(store, "d/e.dat", b"e"))
    assert r.status_code == 201
    assert r.json()["id"] != first["id"]
    assert r.json()["series_id"] == first["series_id"]
    assert r.json()["is_latest"] is True


@pytest.mark.req("API-08")
def test_delete_latest_makes_previous_latest(client: TestClient, store: Path) -> None:
    v1 = _add(client, store, "r/x_v001.cdf", b"1")
    v2 = _add(client, store, "r/x_v002.cdf", b"2")
    assert v2["is_latest"] is True

    r = client.delete(f"/files/{v2['id']}", headers={"X-Forwarded-User": "carol"})
    assert r.status_code == 200
    rec = r.json()
    assert rec["deleted_at"] is not None
    assert rec["updated_by"] == "carol"
    assert rec["is_latest"] is False
    assert rec["state"] == "deleted"
    assert not (store / "r/x_v002.cdf").exists()
    assert client.get(f"/files/{v1['id']}").json()["is_latest"] is True

    assert client.delete(f"/files/{v2['id']}").status_code == 404
    assert client.delete("/files/9999").status_code == 404
    # A deleted file is still readable by id.
    assert client.get(f"/files/{v2['id']}").json()["deleted_at"] is not None


@pytest.mark.req("API-08")
@pytest.mark.req("SVC-15")
def test_delete_store_failure_returns_200_deleting(
    client: TestClient, store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _add(client, store, "r/y.dat")

    def broken(self: StorageLocation, path: str) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(StorageLocation, "rm", broken)
    r = client.delete(f"/files/{rec['id']}")
    assert r.status_code == 200
    assert r.json()["state"] == "deleting"
    assert r.json()["deleted_at"] is None
    assert (store / "r/y.dat").exists()


@pytest.mark.req("API-08")
@pytest.mark.req("SVC-15")
@pytest.mark.req("AUTH-07")
def test_delete_task_errors(
    client: TestClient, store: Path, no_verify: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _add(client, store, "r/z.dat")

    def gone(file_id: int, user: str | None = None) -> None:
        raise models.NotFoundError("raced")

    monkeypatch.setattr(delete_api.delete_file, "fn", gone)
    assert client.delete(f"/files/{rec['id']}").status_code == 404

    def boom(file_id: int, user: str | None = None) -> None:
        raise RuntimeError("backend://user:private-password@host/path?token=private-token")

    # An unexpected error is a 500 (SVC-15), not a 200 with a stale record.
    monkeypatch.setattr(delete_api.delete_file, "fn", boom)
    r = client.delete(f"/files/{rec['id']}")
    assert r.status_code == 500
    assert "RuntimeError" in r.json()["detail"]
    assert "private-" not in r.text
    assert client.get(f"/files/{rec['id']}").json()["deleted_at"] is None


@pytest.mark.req("API-08")
def test_patch_metadata(client: TestClient, store: Path, no_verify: list[int]) -> None:
    rec = _add(client, store, "m/a.dat", metadata={"a": 1, "b": 2})
    url = f"/files/{rec['id']}/metadata"
    r = client.patch(url, json={"b": None, "c": {"x": 1}}, headers={"X-Forwarded-User": "dan"})
    assert r.status_code == 200
    assert r.json()["metadata"] == {"a": 1, "c": {"x": 1}}
    assert r.json()["updated_by"] == "dan"

    r = client.patch(url, params={"replace": "true"}, json={"z": 0, "gone": None})
    assert r.json()["metadata"] == {"z": 0}
    assert r.json()["updated_by"] == "tester"  # the test client's default user header

    assert client.patch(url, json=[1, 2]).status_code == 422
    assert client.patch("/files/9999/metadata", json={}).status_code == 404
    with client.app.state.ctx.write_session() as session:  # type: ignore[attr-defined]
        session.get_one(File, rec["id"]).deleted_at = utcnow()
    assert client.patch(url, json={"a": 1}).status_code == 404


@pytest.mark.req("API-19")
def test_patch_content_date(
    client: TestClient, store: Path, no_verify: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    from heliopuddle.server.api.files import content_date as content_date_api

    notified: list[list[int]] = []
    monkeypatch.setattr(
        content_date_api, "notify_file_changes", lambda _c, _p, ids: notified.append(list(ids))
    )
    dated = _add(client, store, "sci/a_20250304_v2.cdf")
    url = f"/files/{dated['id']}/content-date"
    r = client.patch(
        url, json={"content_date": "2024-01-02T03:04:05Z"}, headers={"X-Forwarded-User": "dan"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["content_date"] == "2024-01-02T03:04:05Z"
    assert r.json()["updated_by"] == "dan"
    assert notified == [[dated["id"]]]
    # A naive time is UTC.
    r = client.patch(url, json={"content_date": "2024-05-06T00:00:00"})
    assert r.json()["content_date"] == "2024-05-06T00:00:00Z"
    # null goes back to the date in the path, or to none.
    assert client.patch(url, json={"content_date": None}).json()["content_date"] == (
        "2025-03-04T00:00:00Z"
    )
    plain = _add(client, store, "m/b.dat", content_date="2024-01-02T03:04:05Z")
    cleared = client.patch(f"/files/{plain['id']}/content-date", json={"content_date": None})
    assert cleared.json()["content_date"] is None

    assert client.patch(url, json={}).status_code == 422
    assert client.patch(url, json={"content_date": "soon"}).status_code == 422
    assert client.patch("/files/9999/content-date", json={"content_date": None}).status_code == 404
    with client.app.state.ctx.write_session() as session:  # type: ignore[attr-defined]
        session.get_one(File, dated["id"]).deleted_at = utcnow()
    assert client.patch(url, json={"content_date": None}).status_code == 404

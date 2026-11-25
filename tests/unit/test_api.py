"""The HTTP API (API-01, API-03, API-05, API-06)."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ByteSize
from sqlalchemy import create_engine

import heliopuddle
from heliopuddle.config import load_projects
from heliopuddle.server.api import create_api
from heliopuddle.server.api.files import register as register_api
from heliopuddle.server.api.health import check as health_api
from heliopuddle.server.context import AppContext, get_context
from heliopuddle.settings import Settings


@pytest.fixture
def client(ctx: AppContext) -> Iterator[TestClient]:
    with TestClient(create_api(ctx)) as c:
        yield c


def _put(store: Path, path: str, data: bytes) -> dict[str, object]:
    target = store / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return {"path": path, "hash": hashlib.md5(data).hexdigest(), "size": len(data)}


# --- API-06: health and version ------------------------------------------


@pytest.mark.req("API-06")
@pytest.mark.usefixtures("prefect_healthy")
def test_health_ok(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "database": "ok", "prefect": "ok"}


@pytest.mark.req("API-06")
def test_health_prefect_unreachable(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health_api, "prefect_api_url", lambda: "http://127.0.0.1:9/api")
    r = client.get("/health")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "error"
    assert body["database"] == "ok"
    assert body["prefect"] == "error"  # no URL or exception text


@pytest.mark.req("API-06")
def test_health_prefect_not_configured(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health_api, "prefect_api_url", lambda: None)
    r = client.get("/health")
    assert r.status_code == 503
    assert r.json()["prefect"] == "error"


@pytest.mark.req("API-06")
@pytest.mark.usefixtures("prefect_healthy")
def test_health_database_unreachable(ctx: AppContext, tmp_path: Path) -> None:
    ctx.engine = create_engine(f"sqlite:///{tmp_path / 'no' / 'such' / 'dir' / 'x.db'}")
    with TestClient(create_api(ctx)) as c:
        r = c.get("/health")
    assert r.status_code == 503
    body = r.json()
    assert body["database"] == "error"
    assert "sqlite" not in r.text.lower()
    assert "no such" not in r.text.lower()
    assert body["prefect"] == "ok"


@pytest.mark.req("API-06")
def test_health_prefect_bad_status(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    class _Resp:
        status_code = 500

    monkeypatch.setattr(health_api, "prefect_api_url", lambda: "http://prefect/api")
    monkeypatch.setattr(health_api.httpx, "get", lambda *a, **k: _Resp())
    r = client.get("/health")
    assert r.status_code == 503
    assert r.json()["prefect"] == "error"
    assert "prefect/api" not in r.text


@pytest.mark.req("API-06")
def test_version(client: TestClient) -> None:
    r = client.get("/version")
    assert r.status_code == 200
    assert r.json() == {"version": heliopuddle.__version__}


# --- API-03: projects and storage locations --------------------------------


@pytest.mark.req("API-03")
def test_projects(client: TestClient) -> None:
    assert client.get("/projects").json() == [
        {"name": "demo", "storage_locations": ["primary", "secondary"]}
    ]
    assert client.get("/projects/demo").json()["name"] == "demo"
    assert client.get("/projects/nope").status_code == 404


@pytest.mark.req("API-03")
def test_storage_locations_list_and_detail(client: TestClient, store: Path) -> None:
    r = client.get("/projects/demo/storage-locations")
    assert r.status_code == 200
    assert r.json()[0] == {
        "name": "primary",
        "url": f"file://{store}",
        "accept_external_changes": True,
        "active": True,
        "create_root": "auto",
        "last_full_index_at": None,
        "last_incremental_index_at": None,
        "quota": None,
        "status": "ok",
        "problems": [],
    }
    assert r.json()[1]["accept_external_changes"] is False
    assert "temp_writes" not in r.json()[1]  # removed (F10)
    assert r.json()[1]["create_root"] is False  # as configured (STO-08)

    detail = client.get("/projects/demo/storage-locations/primary").json()
    assert detail["storage_options"] == {"auto_mkdir": True}
    assert client.get("/projects/demo/storage-locations/nope").status_code == 404
    assert client.get("/projects/nope/storage-locations").status_code == 404
    assert client.get("/projects/nope/storage-locations/primary").status_code == 404


@pytest.mark.req("API-03")
def test_storage_location_options_resolved_from_env(
    settings: Settings, config_dir: Path, store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HP_TEST_ACCOUNT_KEY", "very-secret")
    (config_dir / "demo.yaml").write_text(
        "project: demo\nstorage_locations:\n"
        f"  - name: primary\n    url: file://{store}\n"
        "    storage_options:\n      account_key: ${HP_TEST_ACCOUNT_KEY}\n"
        "      region: ${HP_TEST_REGION:-eu}\n"
    )
    ctx = AppContext.from_settings(settings)
    from heliopuddle.server.database.migrate import upgrade_to_head

    upgrade_to_head(ctx.engine)
    ctx.sync_config()
    with TestClient(create_api(ctx)) as c:
        detail = c.get("/projects/demo/storage-locations/primary").json()
    assert detail["storage_options"] == {"account_key": "very-secret", "region": "eu"}
    ctx.dispose()


@pytest.mark.req("API-03")
def test_inactive_storage_location_listed(ctx: AppContext, config_dir: Path, store: Path) -> None:
    (config_dir / "demo.yaml").write_text(
        f"project: demo\nstorage_locations:\n  - {{name: primary, url: 'file://{store}'}}\n"
    )
    ctx.projects = load_projects(config_dir)
    ctx.sync_config()
    with TestClient(create_api(ctx)) as c:
        listed = {s["name"]: s["active"] for s in c.get("/projects/demo/storage-locations").json()}
        assert listed == {"primary": True, "secondary": False}
        detail = c.get("/projects/demo/storage-locations/secondary").json()
        assert detail["active"] is False
        assert detail["storage_options"] == {}
        assert c.get("/projects/demo").json()["storage_locations"] == ["primary"]
        # Registering into an inactive storage location is refused.
        r = c.post(
            "/projects/demo/files",
            json={"path": "a", "hash": "h", "size": 1, "storage_location": "secondary"},
        )
        assert r.status_code == 404
        assert "not active" in r.json()["detail"]


# --- API-05: config is read-only ------------------------------------------


@pytest.mark.req("API-05")
def test_config_routes_are_get_only(ctx: AppContext) -> None:
    paths: dict[str, dict] = create_api(ctx).openapi()["paths"]
    table = {path: set(ops) for path, ops in paths.items()}
    config_paths = {
        p
        for p in table
        if p.startswith("/projects")
        and "/files" not in p
        and not p.endswith(("/runs", "/uploads", "/summary"))
        and not p.endswith(("/enable", "/disable", "/trigger"))
    }
    assert config_paths == {
        "/projects",
        "/projects/{project}",
        "/projects/{project}/storage-locations",
        "/projects/{project}/storage-locations/{name}",
        "/projects/{project}/rules",
        "/projects/{project}/rules/{name}",
    }
    for path in config_paths:
        assert table[path] == {"get"}, path
    writes = {(p, m) for p, ops in table.items() for m in ops if m not in {"get", "head"}}
    assert writes == {
        ("/auth/token", "post"),
        ("/auth/me/password", "post"),
        ("/users", "post"),
        ("/users/{username}", "patch"),
        ("/users/{username}", "delete"),
        ("/projects/{project}/files", "post"),
        ("/projects/{project}/files/check", "post"),
        ("/files/{file_id}", "delete"),
        ("/files/{file_id}/metadata", "patch"),
        ("/files/{file_id}/content-date", "patch"),
        ("/files/{file_id}/rules-ignore", "patch"),
        ("/projects/{project}/runs", "post"),
        ("/files/{file_id}/accept", "post"),
        ("/projects/{project}/uploads", "post"),
        ("/uploads/{token}/renew", "post"),
        ("/uploads/{token}/content", "put"),
        ("/uploads/{token}/complete", "post"),
        ("/uploads/{token}", "delete"),
        ("/projects/{project}/rules/{name}/enable", "post"),
        ("/projects/{project}/rules/{name}/disable", "post"),
        ("/projects/{project}/rules/{name}/trigger", "post"),
    }


# --- API-01: files ---------------------------------------------------------


@pytest.mark.req("API-01")
def test_register_verifies_inline_and_records_user(client: TestClient, store: Path) -> None:
    body = _put(store, "d/a.dat", b"payload") | {"metadata": {"k": "v"}}
    r = client.post("/projects/demo/files", json=body, headers={"X-Forwarded-User": "alice"})
    assert r.status_code == 201, r.text
    rec = r.json()
    assert rec["state"] == "available"
    assert rec["created_by"] == "alice"
    assert rec["path"] == "d/a.dat"
    assert rec["name"] == "a.dat"
    assert rec["project"] == "demo"
    assert rec["metadata"] == {"k": "v"}
    assert rec["is_latest"] is True
    (copy,) = rec["storage_locations"]
    assert copy["storage_location"] == "primary"
    assert copy["size"] == 7
    assert copy["hash"] == body["hash"]

    got = client.get(f"/files/{rec['id']}")
    assert got.status_code == 200
    assert got.json()["state"] == "available"


@pytest.mark.req("API-01")
def test_register_hash_mismatch_returns_unknown(client: TestClient, store: Path) -> None:
    body = _put(store, "b.dat", b"payload") | {"hash": "0" * 32}
    r = client.post("/projects/demo/files", json=body)
    assert r.status_code == 201
    assert r.json()["state"] == "unknown"
    assert r.json()["created_by"] == "tester"  # the test client's default user header


@pytest.mark.req("API-01")
def test_register_custom_user_header(
    settings: Settings, store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from heliopuddle.server.database.migrate import upgrade_to_head

    ctx = AppContext.from_settings(settings.model_copy(update={"user_header": "X-Remote-User"}))
    upgrade_to_head(ctx.engine)
    ctx.sync_config()
    with TestClient(create_api(ctx)) as c:
        body = _put(store, "c.dat", b"c") | {"storage_location": "primary"}
        r = c.post(
            "/projects/demo/files",
            json=body,
            headers={"X-Remote-User": "bob", "X-Forwarded-User": "mallory"},
        )
        assert r.status_code == 201
        assert r.json()["created_by"] == "bob"
    ctx.dispose()


@pytest.mark.req("API-01")
def test_register_returns_pending_when_verify_raises(
    client: TestClient, store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_copy_id: int) -> None:
        raise RuntimeError("prefect down")

    monkeypatch.setattr(register_api.verify_file, "fn", boom)
    r = client.post("/projects/demo/files", json=_put(store, "p.dat", b"p"))
    assert r.status_code == 201
    assert r.json()["state"] == "pending"


@pytest.mark.req("API-01")
@pytest.mark.parametrize(
    ("project", "body", "status"),
    [
        ("nope", {"path": "a", "hash": "h", "size": 1}, 404),
        ("demo", {"path": "a", "hash": "h", "size": 1, "storage_location": "nope"}, 404),
        ("demo", {"path": "../a", "hash": "h", "size": 1}, 422),
        ("demo", {"path": "/abs", "hash": "h", "size": 1}, 422),
        ("demo", {"path": "a.hp-tmp", "hash": "h", "size": 1}, 422),
        ("demo", {"path": "a", "hash": "h", "size": -1}, 422),
        ("demo", {"path": "a", "size": 1}, 422),
        ("demo", {"path": "a", "hash": "h", "size": 1, "extra": 1}, 422),
    ],
)
def test_register_rejects(client: TestClient, project: str, body: dict, status: int) -> None:
    r = client.post(f"/projects/{project}/files", json=body)
    assert r.status_code == status, r.text


@pytest.mark.req("API-01")
def test_list_files_paging_newest_first(
    client: TestClient, store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(register_api.verify_file, "fn", lambda _id: None)
    for i in range(3):
        assert (
            client.post("/projects/demo/files", json=_put(store, f"f{i}", b"x")).status_code == 201
        )
    page = client.get("/projects/demo/files", params={"limit": 2, "offset": 0}).json()
    assert page["total"] == 3
    assert (page["limit"], page["offset"]) == (2, 0)
    assert [f["path"] for f in page["items"]] == ["f2", "f1"]
    page = client.get("/projects/demo/files", params={"limit": 2, "offset": 2}).json()
    assert [f["path"] for f in page["items"]] == ["f0"]
    assert client.get("/projects/demo/files", params={"limit": 0}).status_code == 422
    assert client.get("/projects/nope/files").status_code == 404
    assert client.get("/files/999").status_code == 404


def test_create_api_without_context_uses_process_context(ctx: AppContext) -> None:
    with TestClient(create_api()) as c:
        assert c.get("/projects").status_code == 200
    assert get_context() is ctx


# --- API-23: upload size cap ----------------------------------------------------------


@pytest.mark.req("API-23")
def test_reservation_and_registration_above_max_upload_size_are_413(
    ctx: AppContext, client: TestClient
) -> None:
    ctx.settings.max_upload_size = ByteSize(1000)
    body = {"path": "big.dat", "hash": "0" * 32, "size": 1001}
    r = client.post("/projects/demo/uploads", json=body)
    assert r.status_code == 413
    assert "HELIOPUDDLE_MAX_UPLOAD_SIZE" in r.json()["detail"]
    assert client.post("/projects/demo/files", json=body).status_code == 413
    ok = client.post("/projects/demo/uploads", json=body | {"size": 1000})
    assert ok.status_code == 200


@pytest.mark.req("API-23")
def test_max_upload_size_default_and_human_readable_value(tmp_path: Path) -> None:
    assert int(Settings(data_dir=tmp_path).max_upload_size) == 1 * 1024**3
    assert int(Settings(data_dir=tmp_path, max_upload_size="2GiB").max_upload_size) == 2 * 1024**3  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Settings(data_dir=tmp_path, max_upload_size=0)  # type: ignore[arg-type]


@pytest.mark.req("API-23")
def test_content_cannot_exceed_the_reserved_size(client: TestClient) -> None:
    data = b"0123456789"
    res = client.post(
        "/projects/demo/uploads",
        json={"path": "small.dat", "hash": hashlib.md5(data).hexdigest(), "size": len(data)},
    ).json()
    r = client.put(f"/uploads/{res['token']}/content", content=data + b"extra")
    assert r.status_code == 413

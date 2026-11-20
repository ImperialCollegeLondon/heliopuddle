"""HelioPuddleClient against the real API in-process (CLI-01, CLI-04)."""

from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from heliopuddle.client import (
    AuthenticationError,
    FileRecord,
    HelioPuddleClient,
    HelioPuddleError,
    NotFoundError,
)
from heliopuddle.client import _uploads as client_module
from heliopuddle.server.api import create_api
from heliopuddle.server.api.files import register as register_api
from heliopuddle.server.api.health import check as health_api
from heliopuddle.server.context import AppContext
from heliopuddle.server.schemas import FileState
from tests.upload_api import requires_upload_api

API_URL = "http://testserver"


@pytest.fixture
def http(ctx: AppContext) -> Iterator[TestClient]:
    with TestClient(create_api(ctx)) as c:
        yield c


@pytest.fixture
def hp(http: TestClient) -> Iterator[HelioPuddleClient]:
    with HelioPuddleClient(API_URL, "demo", user="alice", http_client=http) as c:
        yield c


@pytest.fixture
def no_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the inline verify (copies stay ``pending``)."""
    monkeypatch.setattr(register_api.verify_file, "fn", lambda _id: None)


def _local(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / "local" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


# --- CLI-01: add_file -------------------------------------------------------


@requires_upload_api
@pytest.mark.req("CLI-01")
def test_add_file_writes_and_registers(hp: HelioPuddleClient, tmp_path: Path, store: Path) -> None:
    local = _local(tmp_path, "a.dat", b"payload")
    rec = hp.add_file(local, "d/a.dat", metadata={"k": "v"})

    assert isinstance(rec, FileRecord)
    assert rec.state == FileState.AVAILABLE
    assert rec.created_by == "alice"
    assert rec.path == "d/a.dat"
    assert rec.project == "demo"
    assert rec.size == 7
    assert rec.hash == hashlib.md5(b"payload").hexdigest()
    assert rec.metadata == {"k": "v"}
    assert [c.storage_location for c in rec.storage_locations] == ["primary"]
    assert (store / "d" / "a.dat").read_bytes() == b"payload"
    assert not (store / "d" / "a.dat.hp-tmp").exists()

    assert hp.get_file(rec.id).state == FileState.AVAILABLE
    page = hp.list_files()
    assert page.total == 1
    assert [f.id for f in page.items] == [rec.id]


@requires_upload_api
@pytest.mark.req("CLI-01")
def test_add_file_named_storage_location(
    hp: HelioPuddleClient, tmp_path: Path, ctx: AppContext
) -> None:
    rec = hp.add_file(_local(tmp_path, "b.dat", b"b"), "b.dat", storage_location="secondary")
    assert rec.state == FileState.AVAILABLE
    assert rec.storage_locations[0].storage_location == "secondary"
    assert (tmp_path / "store2" / "b.dat").read_bytes() == b"b"


@requires_upload_api
@pytest.mark.req("CLI-01")
def test_add_file_hash_mismatch_is_unknown(
    hp: HelioPuddleClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_module, "md5_file", lambda _p: "0" * 32)
    rec = hp.add_file(_local(tmp_path, "c.dat", b"c"), "c.dat")
    assert rec.state == FileState.UNKNOWN


@requires_upload_api
@pytest.mark.req("CLI-01")
@pytest.mark.usefixtures("no_verify")
def test_user_header_custom_and_absent(http: TestClient, tmp_path: Path, ctx: AppContext) -> None:
    ctx.settings = ctx.settings.model_copy(update={"user_header": "X-Remote-User"})
    with HelioPuddleClient(
        API_URL, "demo", user="bob", user_header="X-Remote-User", http_client=http
    ) as c:
        assert c.add_file(_local(tmp_path, "u.dat", b"u"), "u.dat").created_by == "bob"
    # The test client's own default header is not the configured one, so a client
    # with no user is refused in header mode (AUTH-20).
    with (
        HelioPuddleClient(API_URL, "demo", http_client=http) as c,
        pytest.raises(AuthenticationError),
    ):
        c.add_file(_local(tmp_path, "v.dat", b"v"), "v.dat")


@pytest.mark.req("CLI-01")
def test_add_file_missing_local_file_touches_nothing(
    hp: HelioPuddleClient, tmp_path: Path, store: Path
) -> None:
    with pytest.raises(FileNotFoundError):
        hp.add_file(tmp_path / "nope.dat", "nope.dat")
    with pytest.raises(FileNotFoundError):
        hp.add_file(tmp_path, "dir.dat")
    assert list(store.iterdir()) == []


@pytest.mark.req("CLI-01")
def test_add_file_unknown_storage_location_or_project(
    hp: HelioPuddleClient, http: TestClient, tmp_path: Path, store: Path
) -> None:
    local = _local(tmp_path, "x.dat", b"x")
    with pytest.raises(NotFoundError) as e:
        hp.add_file(local, "x.dat", storage_location="nope")
    assert e.value.status_code == 404
    assert "nope" in str(e.value.detail)
    with (
        HelioPuddleClient(API_URL, "nope", http_client=http) as other,
        pytest.raises(NotFoundError),
    ):
        other.add_file(local, "x.dat")
    assert list(store.iterdir()) == []


@requires_upload_api
@pytest.mark.req("CLI-01")
@pytest.mark.usefixtures("no_verify")
def test_add_file_rejected_path_is_api_error(hp: HelioPuddleClient, tmp_path: Path) -> None:
    with pytest.raises(HelioPuddleError) as e:
        hp.add_file(_local(tmp_path, "t.dat", b"t"), "t.hp-tmp")
    assert e.value.status_code == 422
    assert not isinstance(e.value, NotFoundError)
    assert isinstance(e.value.detail, list)


# --- CLI-01: reads ----------------------------------------------------------


@requires_upload_api
@pytest.mark.req("CLI-01")
@pytest.mark.usefixtures("no_verify")
def test_list_files_paging_and_get_file(hp: HelioPuddleClient, tmp_path: Path) -> None:
    ids = [hp.add_file(_local(tmp_path, f"f{i}", b"x"), f"f{i}").id for i in range(3)]
    page = hp.list_files(limit=2)
    assert (page.total, page.limit, page.offset) == (3, 2, 0)
    assert [f.path for f in page.items] == ["f2", "f1"]
    assert [f.path for f in hp.list_files(limit=2, offset=2).items] == ["f0"]
    assert hp.get_file(ids[0]).path == "f0"
    with pytest.raises(NotFoundError):
        hp.get_file(999)


@pytest.mark.req("CLI-01")
def test_projects_and_storage_locations(hp: HelioPuddleClient, store: Path) -> None:
    (project,) = hp.list_projects()
    assert project.name == "demo"
    assert project.storage_locations == ["primary", "secondary"]
    assert [s.name for s in hp.list_storage_locations()] == ["primary", "secondary"]

    first = hp.get_storage_location()
    assert first.name == "primary"
    assert first.url == f"file://{store}"
    assert first.storage_options == {"auto_mkdir": True}
    assert hp.get_storage_location("secondary").name == "secondary"
    with pytest.raises(NotFoundError):
        hp.get_storage_location("nope")


@pytest.mark.req("CLI-01")
def test_get_storage_location_project_without_locations(
    http: TestClient, ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = ctx.projects["demo"]
    monkeypatch.setitem(ctx.projects, "demo", cfg.model_copy(update={"storage_locations": []}))
    with (
        HelioPuddleClient(API_URL, "demo", http_client=http) as c,
        pytest.raises(NotFoundError, match="no storage location"),
    ):
        c.get_storage_location()


@pytest.mark.req("CLI-01")
def test_project_none_allows_only_project_agnostic_calls(
    http: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(health_api, "prefect_api_url", lambda: None)
    with HelioPuddleClient(API_URL, None, http_client=http) as c:
        assert [p.name for p in c.list_projects()] == ["demo"]
        health = c.health()  # 503 is a response, not an error
        assert health.status == "error"
        assert health.database == "ok"
        for call in (
            c.list_files,
            c.list_storage_locations,
            c.get_storage_location,
            lambda: c.add_file(_local(tmp_path, "n.dat", b"n"), "n.dat"),
        ):
            with pytest.raises(ValueError, match="project"):
                call()


@pytest.mark.req("CLI-01")
@pytest.mark.usefixtures("prefect_healthy")
def test_health_ok(hp: HelioPuddleClient) -> None:
    assert hp.health().status == "ok"


@pytest.mark.req("CLI-01")
def test_non_json_error_and_owned_http_client() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/files/1"
        assert request.headers["X-Forwarded-User"] == "carol"
        return httpx.Response(500, text="boom")

    http = httpx.Client(base_url="http://h/api/v1", transport=httpx.MockTransport(handler))
    with HelioPuddleClient("http://h/api/v1", "demo", user="carol", http_client=http) as c:
        with pytest.raises(HelioPuddleError) as e:
            c.get_file(1)
        assert (e.value.status_code, e.value.detail) == (500, "boom")
        assert str(e.value) == "500: boom"
    assert not http.is_closed  # not ours to close

    owned = HelioPuddleClient("http://127.0.0.1:9/api/v1/", "demo", timeout=1.0)
    assert str(owned._http.base_url) == "http://127.0.0.1:9/api/v1/"
    owned.close()
    assert owned._http.is_closed


# --- CLI-04: the client never touches the database --------------------------

_CLIENT_SCRIPT = textwrap.dedent(
    """
    import importlib.abc, json, sys

    FORBIDDEN = ("sqlalchemy", "heliopuddle.server.database", "heliopuddle.server.models")

    class Block(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name.startswith(FORBIDDEN):
                raise ImportError(f"client imported {name}")
            return None

    sys.meta_path.insert(0, Block())

    from heliopuddle.client import HelioPuddleClient

    api_url, local = sys.argv[1], sys.argv[2]
    with HelioPuddleClient(api_url, "demo", user="dora") as c:
        rec = c.add_file(local, "iso/a.dat")
        again = c.get_file(rec.id)
    loaded = sorted(m for m in sys.modules if m.startswith(FORBIDDEN))
    print(json.dumps({"state": again.state, "created_by": rec.created_by, "loaded": loaded}))
    """
)


@pytest.fixture
def live_api(ctx: AppContext) -> Iterator[str]:
    """Serve the API on a free local port in a background thread."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_api(ctx), log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "API did not start"
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(10)
    sock.close()


@requires_upload_api
@pytest.mark.req("CLI-04")
def test_add_file_over_http_only_in_separate_process(
    live_api: str, tmp_path: Path, store: Path
) -> None:
    local = _local(tmp_path, "iso.dat", b"isolated")
    script = tmp_path / "client_only.py"
    script.write_text(_CLIENT_SCRIPT)
    result = subprocess.run(
        [sys.executable, str(script), live_api, str(local)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "HELIOPUDDLE_DATA_DIR": str(tmp_path / "unreadable")},
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert out == {"state": "available", "created_by": "dora", "loaded": []}
    assert (store / "iso" / "a.dat").read_bytes() == b"isolated"


@pytest.mark.req("CLI-04")
def test_importing_client_loads_no_database_modules() -> None:
    code = (
        "import sys, heliopuddle.client\n"
        "bad = [m for m in sys.modules if m.split('.')[0] == 'sqlalchemy'"
        " or m.startswith(('heliopuddle.server.database', 'heliopuddle.server.models'))]\n"
        "print(bad)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=True
    )
    assert result.stdout.strip() == "[]"


@requires_upload_api
@pytest.mark.req("STO-01")
@pytest.mark.req("STO-08")
def test_client_writes_with_the_configured_create_root(
    hp: HelioPuddleClient, tmp_path: Path, no_verify: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[object] = []
    real = client_module.StorageLocation

    class Recording(real):  # type: ignore[valid-type, misc]
        def write(self, path: str, source: object) -> None:  # type: ignore[override]
            seen.append(self.create_root)
            super().write(path, source)  # type: ignore[arg-type]

    monkeypatch.setattr(client_module, "StorageLocation", Recording)
    hp.add_file(_local(tmp_path, "t.dat", b"t"), "t.dat", storage_location="secondary")
    assert hp.get_storage_location("secondary").create_root is False
    assert hp.get_storage_location("primary").create_root == "auto"
    assert seen == [False]

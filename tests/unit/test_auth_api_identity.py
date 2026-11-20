"""Identity recorded in the database by every write (AUTH-04, AUTH-11)."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from heliopuddle.server import models
from heliopuddle.server.api import create_api
from heliopuddle.server.api.files import register as register_api
from heliopuddle.server.auth.roles import ROLE_ADMIN
from heliopuddle.server.context import AppContext
from heliopuddle.server.schemas import FileState
from tests.local_auth import add_user, bearer, local_context, login
from tests.unit import _auth_api_helpers as _fixtures

client = _fixtures.client
lctx = _fixtures.lctx


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _write(root: Path, path: str, data: bytes) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def _register(client: TestClient, store: Path, path: str, data: bytes, who: dict[str, str]) -> Any:
    _write(store, path, data)
    r = client.post(
        "/projects/demo/files",
        json={"path": path, "hash": _md5(data), "size": len(data)},
        headers=who,
    )
    assert r.status_code == 201, r.text
    return r.json()


def _unknown_copy(ctx: AppContext, store: Path, path: str, data: bytes) -> int:
    _write(store, path, data)
    with ctx.write_session() as session:
        result = models.register_file(
            session,
            project="demo",
            storage_location="primary",
            path=path,
            hash=_md5(b"registered"),
            size=10,
        )
        models.set_copy_state(session, result.copy_id, FileState.UNKNOWN)
    return result.file_id


def run_every_write(
    ctx: AppContext,
    client: TestClient,
    store: Path,
    identity: Callable[[str], dict[str, str]],
    names: dict[str, str],
) -> None:
    """Each write endpoint, as a different caller, then check the recorded names.

    Args:
        ctx: the app context.
        client: a client for the API.
        store: the ``primary`` storage root.
        identity: request headers authenticating as the given actor.
        names: the username that should be recorded for each actor.
    """
    # register -> created_by
    f1 = _register(client, store, "w/one.dat", b"one", identity("alice"))
    assert f1["created_by"] == names["alice"]

    # reserve and complete a new upload -> created_by
    data = b"two"
    res = client.post(
        "/projects/demo/uploads",
        json={"path": "w/two.dat", "hash": _md5(data), "size": len(data)},
        headers=identity("alice"),
    ).json()
    _write(store, res["write_path"], data)
    done = client.post(f"/uploads/{res['token']}/complete", headers=identity("alice"))
    assert done.status_code == 200, done.text
    assert done.json()["created_by"] == names["alice"]

    # overwrite through uploads -> updated_by
    new = b"two, changed"
    res = client.post(
        "/projects/demo/uploads",
        json={"path": "w/two.dat", "hash": _md5(new), "size": len(new), "overwrite": True},
        headers=identity("bob"),
    ).json()
    _write(store, res["write_path"], new)
    done = client.post(f"/uploads/{res['token']}/complete", headers=identity("bob"))
    assert done.status_code == 200, done.text
    assert (done.json()["created_by"], done.json()["updated_by"]) == (
        names["alice"],
        names["bob"],
    )

    # metadata and rules-ignore -> updated_by
    r = client.patch(f"/files/{f1['id']}/metadata", json={"k": 1}, headers=identity("carol"))
    assert r.status_code == 200 and r.json()["updated_by"] == names["carol"]
    r = client.patch(
        f"/files/{f1['id']}/rules-ignore", json={"ignored": True}, headers=identity("dave")
    )
    assert r.status_code == 200 and r.json()["updated_by"] == names["dave"]

    # accept -> updated_by
    unknown = _unknown_copy(ctx, store, "w/found.dat", b"found")
    r = client.post(f"/files/{unknown}/accept", json={}, headers=identity("erin"))
    assert r.status_code == 200, r.text
    assert r.json()["updated_by"] == names["erin"]

    # delete -> updated_by
    r = client.delete(f"/files/{f1['id']}", headers=identity("frank"))
    assert r.status_code == 200, r.text
    assert r.json()["updated_by"] == names["frank"]


ACTORS = ["alice", "bob", "carol", "dave", "erin", "frank"]


@pytest.mark.req("AUTH-12")
@pytest.mark.req("AUTH-01")
@pytest.mark.usefixtures("inline_prefect")
def test_local_mode_records_the_token_username_and_ignores_a_spoofed_header(
    lctx: AppContext, store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(register_api.verify_file, "fn", lambda i: None)
    for actor in ACTORS:
        add_user(lctx, f"{actor}-user", [ROLE_ADMIN])
    with TestClient(create_api(lctx)) as client:
        tokens = {a: login(client, f"{a}-user") for a in ACTORS}

        def identity(actor: str) -> dict[str, str]:
            return bearer(tokens[actor]) | {"X-Forwarded-User": "mallory"}

        run_every_write(lctx, client, store, identity, {a: f"{a}-user" for a in ACTORS})
        with lctx.sessionmaker() as session:
            from heliopuddle.server.database.orm import File

            names = {f.created_by for f in session.query(File)} | {
                f.updated_by for f in session.query(File)
            }
        assert "mallory" not in names


@pytest.mark.req("AUTH-01")
def test_local_mode_ignores_the_user_header_for_authentication(client: TestClient) -> None:
    r = client.get("/auth/me", headers={"X-Forwarded-User": "ann"})
    assert r.status_code == 401
    r = client.get("/projects", headers={"X-Forwarded-User": "ann"})
    assert r.status_code == 401
    token = login(client, "ann")
    r = client.get("/auth/me", headers=bearer(token) | {"X-Forwarded-User": "root"})
    assert r.json()["username"] == "Ann"


@pytest.mark.req("AUTH-12")
@pytest.mark.usefixtures("inline_prefect")
def test_header_mode_records_the_header_value(
    tmp_path: Path,
    config_dir: Path,
    migrated_db: Path,
    store: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(register_api.verify_file, "fn", lambda i: None)
    ctx = local_context(tmp_path, config_dir, migrated_db, auth_mode="header")
    with TestClient(create_api(ctx)) as client:

        def identity(actor: str) -> dict[str, str]:
            return {"X-Forwarded-User": f"{actor}@proxy"}

        run_every_write(ctx, client, store, identity, {a: f"{a}@proxy" for a in ACTORS})
    ctx.dispose()

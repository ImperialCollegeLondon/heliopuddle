"""The token endpoint of the auth API (AUTH-04, AUTH-11)."""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from heliopuddle.client import HelioPuddleClient, PermissionDeniedError
from heliopuddle.server.api import create_api
from heliopuddle.server.api.auth import token as token_api
from heliopuddle.server.auth import passwords
from heliopuddle.server.auth.roles import ROLE_ADMIN, ROLE_READER, ROLE_WRITER
from heliopuddle.server.context import AppContext
from heliopuddle.server.models import users as users_db
from heliopuddle.server.schemas import FileState
from tests.local_auth import PASSWORD, add_user, bearer, issue_token, local_context
from tests.unit import _auth_api_helpers as _fixtures
from tests.unit._auth_api_helpers import password_form, post_token

client = _fixtures.client
lctx = _fixtures.lctx


@pytest.mark.req("AUTH-07", "AUTH-15", "CLI-01", "CLI-06")
@pytest.mark.parametrize("role", [ROLE_READER, ROLE_WRITER, "hp-writer:demo"])
def test_reader_writer_clients_transfer_without_backing_credentials(
    client: TestClient, lctx: AppContext, tmp_path: Path, role: str, inline_prefect: None
) -> None:
    add_user(lctx, "admin", [ROLE_ADMIN])
    add_user(lctx, "limited", [role])
    source = tmp_path / "source.dat"
    source.write_bytes(b"content")
    admin = HelioPuddleClient("", "demo", token=issue_token(lctx, "admin"), http_client=client)
    record = admin.add_file(source, "original.dat")
    assert admin.download(record.id, tmp_path / "admin-copy.dat").read_bytes() == b"content"
    limited = HelioPuddleClient("", "demo", token=issue_token(lctx, "limited"), http_client=client)
    assert limited.get_file(record.id).hash == record.hash
    assert limited.list_storage_locations()[0].url == ""
    target = tmp_path / "limited-copy.dat"
    with pytest.raises(PermissionDeniedError) as error:
        limited.get_storage_location()
    assert "hp-client-storage-read-write:demo" in error.value.required_roles
    with limited.open(record.id) as content:
        assert content.read() == b"content"
    assert limited.download(record.id, target).read_bytes() == b"content"
    if role == ROLE_READER:
        with pytest.raises(PermissionDeniedError):
            limited.add_file(source, "forbidden.dat")
        assert limited.find().total == 1
    else:
        added = limited.add_file(source, "allowed.dat")
        assert added.state == FileState.AVAILABLE
        assert limited.find().total == 2


@pytest.mark.req("AUTH-07", "AUTH-08", "AUTH-15", "CLI-01", "CLI-06")
@pytest.mark.parametrize("scope", ["", ":demo"])
def test_credential_role_clients_use_direct_transfers(
    client: TestClient, lctx: AppContext, tmp_path: Path, scope: str, inline_prefect: None
) -> None:
    add_user(lctx, "direct", [f"hp-client-storage-read-write{scope}", "hp-writer:demo"])
    paths: list[str] = []
    client.event_hooks["request"].append(lambda request: paths.append(request.url.path))
    direct = HelioPuddleClient("", "demo", token=issue_token(lctx, "direct"), http_client=client)
    assert direct.get_storage_location().storage_options == {"auto_mkdir": True}
    source = tmp_path / "direct.dat"
    source.write_bytes(b"direct content")
    record = direct.add_file(source, "direct.dat")
    assert direct.download(record.id, tmp_path / "copy.dat").read_bytes() == b"direct content"
    assert not any(path.endswith("/content") for path in paths)


@pytest.mark.req("AUTH-04")
def test_token_success_shape_and_login_time(client: TestClient, lctx: AppContext) -> None:
    r = post_token(client, **password_form("ann", PASSWORD))  # case-insensitive
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"access_token", "token_type", "expires_in"}
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 8 * 3600
    assert r.headers["cache-control"] == "no-store"
    with lctx.sessionmaker() as session:
        user = users_db.get_user_by_username(session, "ann")
        assert user is not None
        assert user.last_login_at is not None
    me = client.get("/auth/me", headers=bearer(body["access_token"]))
    assert me.status_code == 200
    assert me.json()["username"] == "Ann"


@pytest.mark.req("AUTH-04")
def test_token_lifetime_follows_the_setting(
    tmp_path: Path, config_dir: Path, migrated_db: Path
) -> None:
    ctx = local_context(tmp_path, config_dir, migrated_db, auth_token_ttl="30m")
    add_user(ctx, "ann")
    with TestClient(create_api(ctx)) as c:
        assert post_token(c, **password_form("ann", PASSWORD)).json()["expires_in"] == 1800
    ctx.dispose()


@pytest.mark.req("AUTH-04")
def test_unknown_wrong_and_disabled_are_indistinguishable(
    client: TestClient, lctx: AppContext
) -> None:
    add_user(lctx, "off", [ROLE_READER], is_active=False)
    responses = [
        post_token(client, **password_form("nobody", PASSWORD)),
        post_token(client, **password_form("ann", "wrong password")),
        post_token(client, **password_form("off", PASSWORD)),
        post_token(client, **password_form("off", "wrong password")),
    ]
    for r in responses:
        assert r.status_code == 400
        assert r.json() == {
            "error": "invalid_grant",
            "error_description": "incorrect username or password",
        }
    with lctx.sessionmaker() as session:
        user = users_db.get_user_by_username(session, "off")
        assert user is not None
        assert user.last_login_at is None


@pytest.mark.req("AUTH-04")
def test_unknown_grant_types_and_missing_fields(client: TestClient) -> None:
    for grant in ("client_credentials", "refresh_token", "authorization_code"):
        r = post_token(client, grant_type=grant, username="ann", password=PASSWORD)
        assert r.status_code == 400
        assert r.json()["error"] == "unsupported_grant_type"
    assert post_token(client, username="ann", password=PASSWORD).json()["error"] == (
        "invalid_request"
    )
    assert post_token(client, grant_type="password", username="ann").json()["error"] == (
        "invalid_request"
    )
    assert client.post("/auth/token", json={"grant_type": "password"}).status_code in (400, 422)


@pytest.mark.req("AUTH-04")
def test_unknown_user_takes_the_dummy_path(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    dummy: list[str] = []
    monkeypatch.setattr(passwords, "verify_dummy", lambda p: dummy.append(p))

    monkeypatch.setattr(token_api, "verify_dummy", passwords.verify_dummy)
    post_token(client, **password_form("nobody", "pw"))
    post_token(client, **password_form("ann", "pw"))
    assert dummy == ["pw"]  # only the unknown user


@pytest.mark.req("AUTH-04")
def test_unknown_and_wrong_password_take_similar_time(client: TestClient) -> None:
    post_token(client, **password_form("nobody", "warm up"))  # build caches

    def mean(username: str, runs: int = 4) -> float:
        start = time.perf_counter()
        for _ in range(runs):
            post_token(client, **password_form(username, "wrong password"))
        return (time.perf_counter() - start) / runs

    unknown, wrong = mean("nobody"), mean("ann")
    assert 1 / 3 < unknown / wrong < 3


@pytest.mark.req("AUTH-04")
def test_passwords_and_tokens_are_never_logged(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        ok = post_token(client, **password_form("ann", PASSWORD))
        post_token(client, **password_form("ann", "a-wrong-secret-pw"))
        client.post(
            "/auth/me/password",
            json={"current_password": PASSWORD, "new_password": "brand new secret!"},
            headers=bearer(ok.json()["access_token"]),
        )
    assert PASSWORD not in caplog.text
    assert "a-wrong-secret-pw" not in caplog.text
    assert "brand new secret!" not in caplog.text
    assert ok.json()["access_token"] not in caplog.text


@pytest.mark.req("AUTH-04")
@pytest.mark.req("AUTH-11")
@pytest.mark.parametrize("mode", ["header", "oidc"])
def test_local_only_endpoints_are_404_in_other_modes(
    tmp_path: Path, config_dir: Path, migrated_db: Path, mode: str
) -> None:
    extra: dict[str, object] = {}
    if mode == "oidc":
        extra = {
            "oidc_issuer": "https://idp.example/realms/r",
            "oidc_client_id": "hp",
            "oidc_client_secret": "s",
        }
    ctx = local_context(tmp_path, config_dir, migrated_db, auth_mode=mode, **extra)
    with TestClient(create_api(ctx)) as c:
        r = post_token(c, **password_form("ann", PASSWORD))
        assert r.status_code == 404
        r = c.post("/auth/me/password", json={"current_password": "x", "new_password": "y" * 12})
        assert r.status_code in (401, 404)
        if mode == "header":
            r = c.post(
                "/auth/me/password",
                json={"current_password": "x", "new_password": "y" * 12},
                headers={"X-Forwarded-User": "ann"},
            )
            assert r.status_code == 404
    ctx.dispose()

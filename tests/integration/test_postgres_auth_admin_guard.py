"""Password changes and the last-admin guard under races on Postgres (AUTH-13)."""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from heliopuddle.server.api import create_api
from heliopuddle.server.auth.passwords import hash_password
from heliopuddle.server.auth.roles import ROLE_ADMIN, ROLE_READER
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import User
from heliopuddle.server.models import users as users_db
from tests.integration import _postgres_auth_helpers as _fixtures
from tests.integration._postgres_auth_helpers import JOIN_TIMEOUT_S, PASSWORD, add_user, run_threads

pg_ctx = _fixtures.pg_ctx


def admin_names(ctx: AppContext) -> list[str]:
    with ctx.sessionmaker() as session:
        return [u.username for u in users_db.list_users(session) if ROLE_ADMIN in u.roles]


@pytest.fixture
def slow_after_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hold each guarded change open for a moment after it took its locks.

    Without ``SELECT ... FOR UPDATE`` the second transaction would read the
    first one's pre-commit state meanwhile, and both changes would pass.
    """
    real = users_db._find_locked  # pyright: ignore[reportPrivateUsage]

    def slow(users: list[User], username: str) -> User:
        found = real(users, username)
        time.sleep(0.3)
        return found

    monkeypatch.setattr(users_db, "_find_locked", slow)


@pytest.mark.req("AUTH-11")
def test_password_change_does_not_overwrite_an_admin_reset_racing_it_on_postgres(
    pg_ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Postgres the user row must be locked before the current password is checked and
    stay locked through the update; otherwise a reset committing in between is overwritten."""
    from heliopuddle.server.api.auth import password as password_api
    from tests.local_auth import bearer, login

    add_user(pg_ctx, "ann", [ROLE_READER])

    def admin_reset() -> None:
        with pg_ctx.write_session() as session:
            user = users_db.get_user_by_username(session, "ann")
            assert user is not None
            users_db.set_password_hash(session, user, hash_password("the admin's reset pw"))

    reset = threading.Thread(target=admin_reset)
    real = password_api.verify_password
    started: list[bool] = []

    def verify_then_race(password: str, hashed: str) -> bool:
        ok = real(password, hashed)
        if not started:
            started.append(True)
            reset.start()
            time.sleep(1.0)  # the reset commits within this window unless the row is locked
        return ok

    with TestClient(create_api(pg_ctx)) as client:
        token = login(client, "ann")
        monkeypatch.setattr(password_api, "verify_password", verify_then_race)
        r = client.post(
            "/auth/me/password",
            json={"current_password": PASSWORD, "new_password": "the user's own new pw"},
            headers=bearer(token),
        )
        assert r.status_code == 204
        reset.join(JOIN_TIMEOUT_S)
        assert not reset.is_alive()
        monkeypatch.undo()
        # The reset came last: it is the password now, and the user's change did not undo it.
        assert (
            client.post(
                "/auth/token",
                data={
                    "grant_type": "password",
                    "username": "ann",
                    "password": "the user's own new pw",
                },
            ).status_code
            == 400
        )
        assert login(client, "ann", "the admin's reset pw")


@pytest.mark.req("AUTH-13")
def test_concurrent_demotions_keep_one_admin_on_postgres(
    pg_ctx: AppContext, slow_after_lock: None
) -> None:
    add_user(pg_ctx, "a", [ROLE_ADMIN])
    add_user(pg_ctx, "b", [ROLE_ADMIN])
    outcomes: list[str] = []

    def demote(i: int) -> None:
        try:
            with pg_ctx.write_session() as session:
                users_db.update_user(session, "ab"[i], roles=[ROLE_READER])
            outcomes.append("ok")
        except users_db.LastAdminError:
            outcomes.append("conflict")

    run_threads(demote, 2)
    assert sorted(outcomes) == ["conflict", "ok"]
    assert len(admin_names(pg_ctx)) == 1


@pytest.mark.req("AUTH-13")
def test_concurrent_mixed_changes_keep_one_admin_on_postgres(
    pg_ctx: AppContext, slow_after_lock: None
) -> None:
    """Delete, disable and demote race over three admins: exactly one survives."""
    for name in ("a", "b", "c"):
        add_user(pg_ctx, name, [ROLE_ADMIN])
    outcomes: list[str] = []

    def change(i: int) -> None:
        try:
            with pg_ctx.write_session() as session:
                if i == 0:
                    users_db.delete_user(session, "a")
                elif i == 1:
                    users_db.update_user(session, "b", is_active=False)
                else:
                    users_db.update_user(session, "c", roles=[])
            outcomes.append("ok")
        except users_db.LastAdminError:
            outcomes.append("conflict")

    run_threads(change, 3)
    assert sorted(outcomes) == ["conflict", "ok", "ok"]
    with pg_ctx.sessionmaker() as session:
        active = [
            u.username
            for u in users_db.list_users(session)
            if u.is_active and ROLE_ADMIN in u.roles
        ]
    assert len(active) == 1


@pytest.mark.req("AUTH-13")
def test_racing_demotions_never_leave_zero_admins_on_postgres(pg_ctx: AppContext) -> None:
    """Without the slowdown, over many rounds: the pair never both succeeds."""
    for round_ in range(15):
        names = (f"a{round_}", f"b{round_}")
        for name in names:
            add_user(pg_ctx, name, [ROLE_ADMIN])
        with pg_ctx.write_session() as session:  # leftovers of earlier rounds go
            for stale in admin_names(pg_ctx):
                if stale not in names:
                    users_db.delete_user(session, stale)
        outcomes: list[str] = []

        def demote(i: int, names: tuple[str, str] = names, outcomes: list[str] = outcomes) -> None:
            try:
                with pg_ctx.write_session() as session:
                    users_db.update_user(session, names[i], roles=[])
                outcomes.append("ok")
            except users_db.LastAdminError:
                outcomes.append("conflict")

        run_threads(demote, 2)
        assert sorted(outcomes) == ["conflict", "ok"], (round_, outcomes)
        assert len(admin_names(pg_ctx)) == 1


@pytest.mark.req("AUTH-13")
def test_users_api_round_trip_on_postgres(pg_ctx: AppContext) -> None:
    add_user(pg_ctx, "Root", [ROLE_ADMIN])
    with TestClient(create_api(pg_ctx)) as client:
        r = client.post(
            "/auth/token",
            data={"grant_type": "password", "username": "root", "password": PASSWORD},
        )
        assert r.status_code == 200, r.text
        auth = {"Authorization": f"Bearer {r.json()['access_token']}"}
        created = client.post(
            "/users",
            json={
                "username": "Bob",
                "password": PASSWORD,
                "roles": [ROLE_READER, "hp-writer:demo"],
            },
            headers=auth,
        )
        assert created.status_code == 201, created.text
        assert (
            client.post(
                "/users", json={"username": "BOB", "password": PASSWORD}, headers=auth
            ).status_code
            == 409
        )
        got = client.get("/users/bOb", headers=auth).json()
        assert got["roles"] == [ROLE_READER, "hp-writer:demo"]
        assert "password_hash" not in got
        patched = client.patch("/users/bob", json={"roles": [], "is_active": False}, headers=auth)
        assert patched.json()["is_active"] is False and patched.json()["roles"] == []
        assert patched.json()["updated_at"] >= got["updated_at"]
        assert client.delete("/users/bob", headers=auth).status_code == 204
        assert client.delete("/users/root", headers=auth).status_code == 409
        assert [u["username"] for u in client.get("/users", headers=auth).json()] == ["Root"]

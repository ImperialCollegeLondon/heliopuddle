"""Users API last-admin guard and concurrency (AUTH-13)."""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from heliopuddle.server.api import create_api
from heliopuddle.server.auth.roles import ROLE_ADMIN, ROLE_READER
from heliopuddle.server.context import AppContext
from heliopuddle.server.models import users as users_db
from tests.local_auth import PASSWORD, add_user, bearer, login
from tests.unit import _users_api_helpers as _fixtures

admin = _fixtures.admin
client = _fixtures.client
lctx = _fixtures.lctx


def _demote_concurrently(ctx: AppContext, names: tuple[str, str]) -> list[str]:
    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def demote(name: str) -> None:
        barrier.wait()
        try:
            with ctx.write_session() as session:
                users_db.update_user(session, name, roles=[])
            outcomes.append("ok")
        except users_db.LastAdminError:
            outcomes.append("conflict")

    threads = [threading.Thread(target=demote, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return outcomes


def users_db_admin_names(ctx: AppContext) -> list[str]:
    with ctx.sessionmaker() as session:
        return [u.username for u in users_db.list_users(session) if ROLE_ADMIN in u.roles]


@pytest.mark.req("AUTH-13")
def test_with_a_second_active_admin_the_first_can_go(
    client: TestClient, admin: dict[str, str]
) -> None:
    assert (
        client.post(
            "/users",
            json={"username": "two", "password": PASSWORD, "roles": [ROLE_ADMIN]},
            headers=admin,
        ).status_code
        == 201
    )
    assert client.patch("/users/root", json={"roles": []}, headers=admin).status_code == 200
    two = bearer(login(client, "two"))
    # now two is the last one
    assert client.patch("/users/two", json={"is_active": False}, headers=two).status_code == 409
    assert client.delete("/users/two", headers=two).status_code == 409


@pytest.mark.req("AUTH-13")
def test_a_disabled_admin_does_not_count(
    client: TestClient, admin: dict[str, str], lctx: AppContext
) -> None:
    add_user(lctx, "sleepy", [ROLE_ADMIN], is_active=False)
    r = client.patch("/users/root", json={"roles": [ROLE_READER]}, headers=admin)
    assert r.status_code == 409
    # but deleting the disabled admin is fine: it is not an active admin
    assert client.delete("/users/sleepy", headers=admin).status_code == 204


@pytest.mark.req("AUTH-13")
def test_an_admin_can_delete_themselves_when_another_exists(
    client: TestClient, admin: dict[str, str], lctx: AppContext
) -> None:
    add_user(lctx, "other", [ROLE_ADMIN])
    assert client.delete("/users/root", headers=admin).status_code == 204


@pytest.mark.req("AUTH-13")
def test_concurrent_demotions_leave_one_admin(lctx: AppContext) -> None:
    """Two admins demote each other at once: exactly one change wins (atomic guard)."""
    for round_ in range(5):
        a, b = f"a{round_}", f"b{round_}"
        add_user(lctx, a, [ROLE_ADMIN])
        add_user(lctx, b, [ROLE_ADMIN])
        assert sorted(_demote_concurrently(lctx, (a, b))) == ["conflict", "ok"]
        assert len(users_db_admin_names(lctx)) == 1
        for name in users_db_admin_names(lctx):
            with lctx.write_session() as session:
                user = users_db.get_user_by_username(session, name)
                assert user is not None
                session.delete(user)


@pytest.mark.req("AUTH-13")
def test_concurrent_api_demotions_never_remove_every_admin(
    lctx: AppContext,
) -> None:
    add_user(lctx, "a", [ROLE_ADMIN])
    add_user(lctx, "b", [ROLE_ADMIN])
    statuses: list[int] = []
    barrier = threading.Barrier(2)
    with TestClient(create_api(lctx)) as setup:
        tokens = {n: login(setup, n) for n in ("a", "b")}

    def demote(actor: str, target: str) -> None:
        with TestClient(create_api(lctx)) as c:
            barrier.wait()
            r = c.patch(f"/users/{target}", json={"roles": []}, headers=bearer(tokens[actor]))
            statuses.append(r.status_code)

    threads = [threading.Thread(target=demote, args=p) for p in (("a", "b"), ("b", "a"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert statuses.count(200) == 1, statuses
    assert set(statuses) <= {200, 403, 409}  # 403: the loser already lost its admin role
    assert len(users_db_admin_names(lctx)) == 1


@pytest.mark.req("AUTH-13")
def test_a_rejected_password_is_not_echoed_in_the_422(
    client: TestClient, admin: dict[str, str]
) -> None:
    secret = "tooshort"
    r = client.post("/users", json={"username": "x", "password": secret}, headers=admin)
    assert r.status_code == 422
    assert "at least 12" in r.text
    assert secret not in r.text
    r = client.patch("/users/reader", json={"password": secret}, headers=admin)
    assert r.status_code == 422
    assert secret not in r.text
    r = client.post(
        "/auth/me/password",
        json={"current_password": PASSWORD, "new_password": secret},
        headers=admin,
    )
    assert r.status_code == 422
    assert secret not in r.text and PASSWORD not in r.text

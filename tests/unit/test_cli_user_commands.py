"""``heliopuddle user`` add, list, show, set, disable and delete."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from heliopuddle.server.auth.roles import ROLE_ADMIN
from heliopuddle.server.context import AppContext
from heliopuddle.server.models import users as users_db
from tests.local_auth import PASSWORD, add_user, bearer, login
from tests.unit import _cli_user_helpers as _fixtures
from tests.unit._cli_user_helpers import NEW, as_root, run

_clean_env = _fixtures._clean_env
api = _fixtures.api
lctx = _fixtures.lctx


def user_row(ctx: AppContext, name: str):  # type: ignore[no-untyped-def]
    with ctx.sessionmaker() as session:
        user = users_db.get_user_by_username(session, name)
        if user is not None:
            session.expunge(user)
        return user


@pytest.mark.req("AUTH-14")
def test_add_with_a_prompted_password_and_roles(api: TestClient, lctx: AppContext) -> None:
    r = run(
        "add",
        "Bob",
        "--roles",
        "hp-reader, hp-writer:demo",
        "--token",
        login(api, "root"),
        input=f"{NEW}\n{NEW}\n",
    )
    assert r.exit_code == 0, r.output
    assert "Created user 'Bob'" in r.stdout
    assert "hp-reader, hp-writer:demo" in r.stdout
    assert NEW not in r.output
    user = user_row(lctx, "bob")
    assert user is not None
    assert user.roles == ["hp-reader", "hp-writer:demo"]
    assert user.is_active
    assert login(api, "bob", NEW)


@pytest.mark.req("AUTH-14")
def test_add_prompt_mismatch_aborts(api: TestClient, lctx: AppContext) -> None:
    r = run(
        "add", "bob", "--token", login(api, "root"), input=f"{NEW}\nsomething else entirely\n" * 3
    )
    assert r.exit_code != 0
    assert user_row(lctx, "bob") is None


@pytest.mark.req("AUTH-14")
def test_add_with_new_password_stdin_and_env(api: TestClient, lctx: AppContext) -> None:
    token = login(api, "root")
    r = run(
        "add", "carol", "--new-password-stdin", "--token", token, "--disabled", input=NEW + "\n"
    )
    assert r.exit_code == 0, r.output
    carol = user_row(lctx, "carol")
    assert carol is not None and not carol.is_active and carol.roles == []
    r = run("add", "dave", "--token", token, env={"HELIOPUDDLE_NEW_PASSWORD": NEW})
    assert r.exit_code == 0, r.output
    assert login(api, "dave", NEW)


@pytest.mark.req("AUTH-14")
def test_add_cannot_read_both_passwords_from_stdin(api: TestClient) -> None:
    r = as_root("add", "bob", "--new-password-stdin", input=PASSWORD + "\n" + NEW + "\n")
    assert r.exit_code == 2
    assert "cannot both read stdin" in r.stderr


@pytest.mark.req("AUTH-14")
def test_add_failures_duplicate_short_password_and_bad_role(api: TestClient) -> None:
    token = login(api, "root")
    r = run("add", "ROOT", "--token", token, env={"HELIOPUDDLE_NEW_PASSWORD": NEW})
    assert r.exit_code == 1
    assert "already exists" in r.stderr
    r = run("add", "x", "--token", token, env={"HELIOPUDDLE_NEW_PASSWORD": "short"})
    assert r.exit_code == 1
    assert "at least 12" in r.stderr
    assert "short" not in r.stderr.replace("at least", "")
    r = run(
        "add", "x", "--roles", "hp-nope", "--token", token, env={"HELIOPUDDLE_NEW_PASSWORD": NEW}
    )
    assert r.exit_code == 1
    assert "invalid role" in r.stderr
    assert "Traceback" not in r.output


@pytest.mark.req("AUTH-14")
def test_list_prints_a_table_without_secrets(api: TestClient) -> None:
    api.get("/auth/me", headers=bearer(login(api, "ann")))
    r = as_root("list", input=PASSWORD + "\n")
    assert r.exit_code == 0
    lines = r.stdout.splitlines()
    assert lines[0].split() == ["USERNAME", "ACTIVE", "ROLES", "LAST", "LOGIN"]
    by_name = {ln.split()[0]: ln for ln in lines[1:]}
    assert set(by_name) == {"ann", "root"}
    assert ROLE_ADMIN in by_name["root"] and "yes" in by_name["root"]
    assert "UTC" in by_name["ann"]
    assert "$argon2" not in r.output and "password" not in r.stdout.lower()


@pytest.mark.req("AUTH-14")
def test_show(api: TestClient) -> None:
    token = login(api, "root")
    r = run("show", "ANN", "--token", token)
    assert r.exit_code == 0, r.output
    assert "Username:    ann" in r.stdout
    assert "Roles:       hp-reader" in r.stdout
    assert "Active:      yes" in r.stdout
    assert "Last login:  never" in r.stdout
    assert "$argon2" not in r.output
    r = run("show", "ghost", "--token", token)
    assert r.exit_code == 1
    assert r.stderr.strip() == "Error: user 'ghost' not found"


@pytest.mark.req("AUTH-14")
def test_set_password_prompt_stdin_and_env(api: TestClient) -> None:
    token = login(api, "root")
    r = run("set-password", "ann", "--token", token, input=f"{NEW}\n{NEW}\n")
    assert r.exit_code == 0, r.output
    assert "Password changed for 'ann'" in r.stdout
    assert NEW not in r.output
    assert login(api, "ann", NEW)
    second = "second long password"
    assert (
        run(
            "set-password", "ann", "--token", token, "--new-password-stdin", input=second + "\n"
        ).exit_code
        == 0
    )
    assert login(api, "ann", second)
    third = "third long password"
    r = run("set-password", "ann", "--token", token, env={"HELIOPUDDLE_NEW_PASSWORD": third})
    assert r.exit_code == 0
    assert login(api, "ann", third)
    r = run("set-password", "ghost", "--token", token, env={"HELIOPUDDLE_NEW_PASSWORD": third})
    assert r.exit_code == 1
    assert "user 'ghost' not found" in r.stderr
    r = run("set-password", "ann", "--token", token, env={"HELIOPUDDLE_NEW_PASSWORD": "tiny"})
    assert r.exit_code == 1
    assert "at least 12" in r.stderr


@pytest.mark.req("AUTH-14")
def test_set_roles_replaces_and_clears(api: TestClient, lctx: AppContext) -> None:
    token = login(api, "root")
    r = run("set-roles", "ann", "--roles", "hp-writer,hp-deleter:demo", "--token", token)
    assert r.exit_code == 0, r.output
    assert "hp-writer, hp-deleter:demo" in r.stdout
    user = user_row(lctx, "ann")
    assert user is not None and user.roles == ["hp-writer", "hp-deleter:demo"]
    r = run("set-roles", "ann", "--roles", "", "--token", token)
    assert r.exit_code == 0
    assert "(none)" in r.stdout
    r = run("set-roles", "ann", "--roles", "wizard", "--token", token)
    assert r.exit_code == 1
    assert "invalid role" in r.stderr
    assert run("set-roles", "ann", "--token", token).exit_code == 2  # --roles is required


@pytest.mark.req("AUTH-14")
def test_disable_and_enable(api: TestClient, lctx: AppContext) -> None:
    token = login(api, "root")
    r = run("disable", "ann", "--token", token)
    assert r.exit_code == 0
    assert "disabled" in r.stdout
    user = user_row(lctx, "ann")
    assert user is not None and not user.is_active
    r = run("enable", "ann", "--token", token)
    assert r.exit_code == 0
    assert "enabled" in r.stdout
    user = user_row(lctx, "ann")
    assert user is not None and user.is_active


@pytest.mark.req("AUTH-14")
def test_delete_asks_first_and_yes_skips_the_question(api: TestClient, lctx: AppContext) -> None:
    token = login(api, "root")
    r = run("delete", "ann", "--token", token, input="n\n")
    assert r.exit_code == 1
    assert user_row(lctx, "ann") is not None
    r = run("delete", "ann", "--token", token, input="y\n")
    assert r.exit_code == 0, r.output
    assert "Deleted user 'ann'" in r.stdout
    assert user_row(lctx, "ann") is None
    add_user(lctx, "eve")
    assert run("delete", "eve", "-y", "--token", token).exit_code == 0
    assert user_row(lctx, "eve") is None
    r = run("delete", "eve", "--yes", "--token", token)
    assert r.exit_code == 1
    assert "user 'eve' not found" in r.stderr

"""First-time admin and recovery (AUTH-03)."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import pytest
from pydantic import ValidationError

from heliopuddle.server.auth.bootstrap import ensure_admin
from heliopuddle.server.auth.passwords import verify_password
from heliopuddle.server.auth.roles import ROLE_ADMIN, ROLE_READER
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import utcnow
from heliopuddle.server.models import users as users_db
from heliopuddle.settings import Settings
from tests.local_auth import local_context

ENV_PASSWORD = "an-env-password-1"


@pytest.fixture
def make_ctx(tmp_path: Path, config_dir: Path, migrated_db: Path):  # type: ignore[no-untyped-def]
    made: list[AppContext] = []

    def make(**settings: object) -> AppContext:
        ctx = local_context(tmp_path, config_dir, migrated_db, **settings)
        made.append(ctx)
        return ctx

    yield make
    for ctx in made:
        ctx.dispose()


def all_users(ctx: AppContext) -> list[tuple[str, list[str], bool]]:
    with ctx.sessionmaker() as session:
        return [(u.username, u.roles, u.is_active) for u in users_db.list_users(session)]


@pytest.mark.req("AUTH-03")
def test_env_username_and_password_create_the_admin(make_ctx) -> None:  # type: ignore[no-untyped-def]
    ctx = make_ctx(admin_username="boss", admin_password=ENV_PASSWORD)
    ensure_admin(ctx)
    assert all_users(ctx) == [("boss", [ROLE_ADMIN], True)]
    with ctx.sessionmaker() as session:
        user = users_db.get_user_by_username(session, "BOSS")
        assert user is not None
        assert verify_password(ENV_PASSWORD, user.password_hash)
        assert user.password_hash != ENV_PASSWORD


@pytest.mark.req("AUTH-03")
def test_generated_password_is_random_and_logged_once(
    make_ctx,
    caplog: pytest.LogCaptureFixture,  # type: ignore[no-untyped-def]
) -> None:
    ctx = make_ctx()
    with caplog.at_level(logging.INFO, logger="heliopuddle"):
        ensure_admin(ctx)
        ensure_admin(ctx)  # a restart
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "'admin'" in message
    generated = message.split("generated password ")[1].split()[0]
    assert len(generated) == 24
    assert all(c.isalnum() or c in "-_" for c in generated)
    with ctx.sessionmaker() as session:
        user = users_db.get_user_by_username(session, "admin")
        assert user is not None
        assert verify_password(generated, user.password_hash)


@pytest.mark.req("AUTH-03")
def test_generated_passwords_differ_and_are_long_enough() -> None:
    from heliopuddle.server.auth.bootstrap import generate_password
    from heliopuddle.settings import MIN_PASSWORD_LENGTH

    passwords = {generate_password() for _ in range(20)}
    assert len(passwords) == 20
    assert all(len(p) == 24 >= MIN_PASSWORD_LENGTH for p in passwords)


@pytest.mark.req("AUTH-03")
def test_env_password_is_never_logged(
    make_ctx,
    caplog: pytest.LogCaptureFixture,  # type: ignore[no-untyped-def]
) -> None:
    ctx = make_ctx(admin_password=ENV_PASSWORD)
    with caplog.at_level(logging.DEBUG):
        ensure_admin(ctx)
    assert ENV_PASSWORD not in caplog.text


@pytest.mark.req("AUTH-03")
def test_restart_does_not_reset_the_password_or_other_users(make_ctx) -> None:  # type: ignore[no-untyped-def]
    ctx = make_ctx(admin_password=ENV_PASSWORD)
    ensure_admin(ctx)
    with ctx.write_session() as session:
        user = users_db.get_user_by_username(session, "admin")
        assert user is not None
        users_db.set_password_hash(session, user, "changed-hash")
        users_db.set_roles(session, user, [ROLE_READER])
    ensure_admin(ctx)
    with ctx.sessionmaker() as session:
        user = users_db.get_user_by_username(session, "admin")
        assert user is not None
        assert user.password_hash == "changed-hash"
        assert user.roles == [ROLE_READER]
        assert users_db.count_users(session) == 1


@pytest.mark.req("AUTH-03")
def test_env_vars_do_not_apply_once_any_user_exists(make_ctx) -> None:  # type: ignore[no-untyped-def]
    ctx = make_ctx(admin_username="boss", admin_password=ENV_PASSWORD)
    with ctx.write_session() as session:
        users_db.create_user(session, "someone", "hash", [ROLE_READER])
    ensure_admin(ctx)
    assert all_users(ctx) == [("someone", [ROLE_READER], True)]


@pytest.mark.req("AUTH-03")
def test_concurrent_starts_create_exactly_one_admin(
    make_ctx,
    caplog: pytest.LogCaptureFixture,  # type: ignore[no-untyped-def]
) -> None:
    ctx = make_ctx()
    errors: list[BaseException] = []
    barrier = threading.Barrier(6)

    def start() -> None:
        try:
            barrier.wait()
            ensure_admin(ctx)
        except BaseException as e:
            errors.append(e)

    with caplog.at_level(logging.WARNING, logger="heliopuddle"):
        threads = [threading.Thread(target=start) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert errors == []
    assert all_users(ctx) == [("admin", [ROLE_ADMIN], True)]
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


@pytest.mark.req("AUTH-03")
def test_a_lost_race_is_treated_as_already_created(
    make_ctx,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,  # type: ignore[no-untyped-def]
) -> None:
    """On Postgres the loser sees ``count == 0`` and then a unique violation."""
    ctx = make_ctx()
    with ctx.write_session() as session:
        users_db.create_user(session, "admin", "hash", [ROLE_ADMIN])
    monkeypatch.setattr(users_db, "count_users", lambda session: 0)
    with caplog.at_level(logging.WARNING, logger="heliopuddle"):
        ensure_admin(ctx)
    assert caplog.records == []
    assert all_users(ctx) == [("admin", [ROLE_ADMIN], True)]


@pytest.mark.req("AUTH-03")
def test_short_password_stops_startup(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="at least 12"):
        Settings(config_dir=tmp_path, data_dir=tmp_path, auth_mode="local", admin_password="short")  # type: ignore[arg-type]


@pytest.mark.req("AUTH-03")
def test_reset_needs_the_password(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="HELIOPUDDLE_ADMIN_PASSWORD"):
        Settings(config_dir=tmp_path, data_dir=tmp_path, auth_mode="local", admin_reset=True)


@pytest.mark.req("AUTH-03")
def test_reset_recovers_a_lost_admin(
    make_ctx,
    tmp_path: Path,
    config_dir: Path,
    migrated_db: Path,  # type: ignore[no-untyped-def]
    caplog: pytest.LogCaptureFixture,
) -> None:
    ctx = make_ctx(admin_password=ENV_PASSWORD)
    ensure_admin(ctx)
    before = utcnow()
    with ctx.write_session() as session:
        user = users_db.get_user_by_username(session, "admin")
        assert user is not None
        users_db.set_password_hash(session, user, "forgotten")
        users_db.set_active(session, user, False)
        users_db.set_roles(session, user, [ROLE_READER])
    ctx.settings.admin_reset = True  # as if started with HELIOPUDDLE_ADMIN_RESET=true
    with caplog.at_level(logging.INFO, logger="heliopuddle"):
        ensure_admin(ctx)
    assert all_users(ctx) == [("admin", [ROLE_READER, ROLE_ADMIN], True)]
    with ctx.sessionmaker() as session:
        user = users_db.get_user_by_username(session, "admin")
        assert user is not None
        assert verify_password(ENV_PASSWORD, user.password_hash)
        assert user.credentials_changed_at >= before
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "ADMIN_RESET" in warnings[0].getMessage()
    assert ENV_PASSWORD not in caplog.text


@pytest.mark.req("AUTH-03")
def test_reset_creates_a_missing_admin(make_ctx) -> None:  # type: ignore[no-untyped-def]
    ctx = make_ctx(admin_password=ENV_PASSWORD, admin_reset=True, admin_username="root")
    with ctx.write_session() as session:
        users_db.create_user(session, "other", "hash", [ROLE_READER])
    ensure_admin(ctx)
    assert all_users(ctx) == [("other", [ROLE_READER], True), ("root", [ROLE_ADMIN], True)]


@pytest.mark.req("AUTH-03")
def test_concurrent_starts_with_different_admin_usernames_still_create_one_admin(
    make_ctx,
    tmp_path: Path,  # type: ignore[no-untyped-def]
) -> None:
    """Replicas configured with different ``HELIOPUDDLE_ADMIN_USERNAME`` values cannot
    both create an admin: there is no unique-name conflict to stop them."""
    first = make_ctx(admin_username="admin0", admin_password=ENV_PASSWORD)
    contexts = [first] + [
        AppContext.from_settings(first.settings.model_copy(update={"admin_username": f"admin{i}"}))
        for i in range(1, 6)
    ]
    barrier = threading.Barrier(len(contexts))
    errors: list[BaseException] = []

    def start(ctx: AppContext) -> None:
        try:
            barrier.wait()
            ensure_admin(ctx)
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=start, args=(c,)) for c in contexts]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors, errors
    assert len(all_users(first)) == 1
    for c in contexts[1:]:
        c.dispose()

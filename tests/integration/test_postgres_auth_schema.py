"""Local user table and username uniqueness on a real Postgres (AUTH-02, AUTH-03)."""

from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from heliopuddle.server.auth.roles import ROLE_READER
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.engine import create_engine_for
from heliopuddle.server.database.migrate import (
    alembic_config,
    current_revision,
    head_revision,
    schema_diffs,
    upgrade_to_head,
)
from heliopuddle.server.database.orm import User
from heliopuddle.server.models import users as users_db
from tests.integration import _postgres_auth_helpers as _fixtures
from tests.integration._postgres_auth_helpers import add_user, run_threads

pg_ctx = _fixtures.pg_ctx


USER_COLUMNS = {
    "id",
    "username",
    "password_hash",
    "roles",
    "is_active",
    "credentials_changed_at",
    "created_at",
    "updated_at",
    "last_login_at",
}


@pytest.mark.req("AUTH-02")
def test_migration_0006_creates_the_user_table_on_postgres(postgres_url: str) -> None:
    upgrade_to_head(postgres_url)
    assert current_revision(postgres_url) == head_revision()
    assert schema_diffs(postgres_url) == []

    engine = create_engine_for(postgres_url)
    try:
        insp = inspect(engine)
        assert "user" in insp.get_table_names()
        assert {c["name"] for c in insp.get_columns("user")} == USER_COLUMNS
        index = {i["name"]: i for i in insp.get_indexes("user")}["ux_user_username_lower"]
        assert index["unique"]
        with Session(engine) as session:
            session.add(
                User(username="Alice", password_hash="h", roles=["hp-reader", "hp-writer:x"])
            )
            session.commit()
            user = session.query(User).one()
            assert user.roles == ["hp-reader", "hp-writer:x"]
            assert user.is_active is True
            assert user.created_at.utcoffset() is not None
    finally:
        engine.dispose()


@pytest.mark.req("AUTH-02")
def test_migration_downgrades_and_upgrades_again_on_postgres(postgres_url: str) -> None:
    upgrade_to_head(postgres_url)
    cfg = alembic_config()
    cfg.set_main_option("sqlalchemy.url", postgres_url)
    engine = create_engine_for(postgres_url)
    try:
        with engine.begin() as connection:
            cfg.attributes["connection"] = connection
            command.downgrade(cfg, "base")
        assert "user" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()
    upgrade_to_head(postgres_url)
    assert "user" in inspect(create_engine_for(postgres_url)).get_table_names()
    assert current_revision(postgres_url) == head_revision()


@pytest.mark.req("AUTH-02")
def test_usernames_are_unique_ignoring_case_on_postgres(pg_ctx: AppContext) -> None:
    add_user(pg_ctx, "Alice", [ROLE_READER])
    with pg_ctx.sessionmaker() as session:
        session.add(User(username="aLICE", password_hash="h"))
        with pytest.raises(IntegrityError):
            session.commit()
    with pg_ctx.write_session() as session:
        with pytest.raises(users_db.UserExistsError):
            users_db.create_user(session, "ALICE", "h")
        # the savepoint kept the session usable
        users_db.create_user(session, "bob", "h")
    with pg_ctx.sessionmaker() as session:
        found = users_db.get_user_by_username(session, "aLiCe")
        assert found is not None and found.username == "Alice"
        assert [u.username for u in users_db.list_users(session)] == ["Alice", "bob"]


@pytest.mark.req("AUTH-02")
def test_concurrent_creates_of_one_username_leave_one_row(pg_ctx: AppContext) -> None:
    outcomes: list[str] = []

    def create(i: int) -> None:
        try:
            with pg_ctx.write_session() as session:
                users_db.create_user(session, "Same" if i % 2 else "SAME", "h")
            outcomes.append("ok")
        except users_db.UserExistsError:
            outcomes.append("exists")

    run_threads(create, 8)
    assert sorted(outcomes) == ["exists"] * 7 + ["ok"]
    with pg_ctx.sessionmaker() as session:
        assert users_db.count_users(session) == 1

"""The ``user`` table and its migration (AUTH-02)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from heliopuddle.server.database.engine import create_engine_for
from heliopuddle.server.database.migrate import (
    schema_diffs,
    upgrade_to_head,
)
from heliopuddle.server.database.orm import User


@pytest.mark.req("AUTH-02")
def test_migration_creates_the_user_table_matching_the_model(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'hp.db'}"
    upgrade_to_head(url)
    assert schema_diffs(url) == []
    engine = create_engine_for(url)
    insp = inspect(engine)
    assert "user" in insp.get_table_names()
    assert {c["name"] for c in insp.get_columns("user")} == {
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
    engine.dispose()


@pytest.mark.req("AUTH-02")
def test_username_is_unique_ignoring_case_and_roles_round_trip(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'hp.db'}"
    upgrade_to_head(url)
    engine = create_engine_for(url)
    with Session(engine) as session:
        session.add(
            User(username="Alice", password_hash="h", roles=["hp-reader", "hp-writer:demo"])
        )
        session.commit()
        user = session.scalars(select(User)).one()
        assert user.roles == ["hp-reader", "hp-writer:demo"]
        assert user.is_active is True
        assert user.created_at.tzinfo is UTC
        assert user.credentials_changed_at <= datetime.now(UTC)
        assert user.last_login_at is None
        session.add(User(username="aLICE", password_hash="h2", roles=[]))
        with pytest.raises(IntegrityError):
            session.commit()
    engine.dispose()

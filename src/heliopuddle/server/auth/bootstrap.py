"""First-time admin and recovery (AUTH-03)."""

from __future__ import annotations

import logging
import secrets

from heliopuddle.server.auth.passwords import hash_password
from heliopuddle.server.auth.roles import ROLE_ADMIN
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.migrate import lock_schema
from heliopuddle.server.models import users as users_db

logger = logging.getLogger(__name__)

GENERATED_PASSWORD_LENGTH = 24


def generate_password() -> str:
    """Return a random URL-safe password of :data:`GENERATED_PASSWORD_LENGTH` characters."""
    return secrets.token_urlsafe(GENERATED_PASSWORD_LENGTH)[:GENERATED_PASSWORD_LENGTH]


def ensure_admin(ctx: AppContext) -> None:
    """Create the first admin if there are no users; apply ``admin_reset`` if requested.

    With no users, one active ``hp-admin`` is created: its username is
    ``settings.admin_username``, its password ``settings.admin_password`` or,
    if unset, a random one that is logged once at WARNING. The env password is
    used only while no users exist, so a restart never resets a password.
    Concurrent starts create exactly one admin, whatever username each is
    configured with: the startup advisory lock (Postgres; SQLite's write
    transaction already serialises) is taken before the users are counted, so a
    second start sees the first one's admin. A start that loses a unique
    username conflict anyway treats it as already created, and logs nothing.

    With ``settings.admin_reset`` (which needs ``admin_password``), that user's
    password is reset, the user is re-enabled and ``hp-admin`` is restored,
    creating the user if missing; a WARNING is logged.
    """
    settings = ctx.settings
    username = settings.admin_username
    if settings.admin_reset and settings.admin_password is not None:
        _reset_admin(ctx, username, settings.admin_password.get_secret_value())
        return
    with ctx.write_session() as session:
        lock_schema(session.connection())
        if users_db.count_users(session) > 0:
            return
        if settings.admin_password is not None:
            password, generated = settings.admin_password.get_secret_value(), False
        else:
            password, generated = generate_password(), True
        try:
            users_db.create_user(session, username, hash_password(password), [ROLE_ADMIN])
        except users_db.UserExistsError:
            return  # a concurrent start created it
    if generated:
        logger.warning(
            "created the first admin %r with the generated password %s "
            "(shown once; change it, or set HELIOPUDDLE_ADMIN_PASSWORD before the first start)",
            username,
            password,
        )
    else:
        logger.info("created the first admin %r from HELIOPUDDLE_ADMIN_PASSWORD", username)


def _reset_admin(ctx: AppContext, username: str, password: str) -> None:
    password_hash = hash_password(password)
    with ctx.write_session() as session:
        lock_schema(session.connection())
        user = users_db.get_user_by_username(session, username, for_update=True)
        if user is None:
            users_db.create_user(session, username, password_hash, [ROLE_ADMIN])
        else:
            users_db.set_password_hash(session, user, password_hash)
            users_db.set_active(session, user, True)
            users_db.set_roles(session, user, [*user.roles, ROLE_ADMIN])
    logger.warning(
        "HELIOPUDDLE_ADMIN_RESET: reset the password of %r, enabled it and restored %s; "
        "remove HELIOPUDDLE_ADMIN_RESET and HELIOPUDDLE_ADMIN_PASSWORD from the environment",
        username,
        ROLE_ADMIN,
    )

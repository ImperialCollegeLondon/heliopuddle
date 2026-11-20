"""Local users (M5A, AUTH-02, AUTH-03, AUTH-05): DB access functions.

Usernames are ASCII (``USERNAME_PATTERN``) and unique ignoring case (the
``ux_user_username_lower`` index); lookups compare lower-cased values, which SQL
and Python agree on for ASCII only. Writers take a session from
``ctx.write_session()``; the caller commits. Password hashing lives in
:mod:`heliopuddle.server.auth.passwords`, so these functions only ever see
hashes.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from heliopuddle.server.auth.roles import ROLE_ADMIN
from heliopuddle.server.database.orm import User, utcnow


class UserExistsError(Exception):
    """A user with this username (ignoring case) already exists."""


class UserNotFoundError(Exception):
    """No user has this username (ignoring case)."""


class LastAdminError(Exception):
    """The change would leave no active user holding ``hp-admin``."""


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def credentials_version(user: User) -> int:
    """The user's credentials version: ``credentials_changed_at`` in whole microseconds.

    A token carries the version current when it was issued and is valid only
    while it still equals the user's (AUTH-05), so a password change, a reset
    or a recreated user invalidates every earlier token at once, whatever the
    clock resolution of ``iat``.
    """
    return (user.credentials_changed_at.astimezone(UTC) - _EPOCH) // timedelta(microseconds=1)


def get_user_by_id(session: Session, user_id: int, *, for_update: bool = False) -> User | None:
    """Return the user with this id, or None; ``for_update`` locks the row (Postgres)."""
    stmt = select(User).where(User.id == user_id)
    if for_update:
        stmt = stmt.with_for_update()
    return session.scalar(stmt)


def get_user_by_username(
    session: Session, username: str, *, for_update: bool = False
) -> User | None:
    """Return the user with ``username`` (case-insensitive), or None.

    ``for_update`` locks the row until the transaction ends (Postgres; on SQLite
    the write transaction already serialises writers).
    """
    stmt = select(User).where(func.lower(User.username) == username.lower())
    if for_update:
        stmt = stmt.with_for_update()
    return session.scalar(stmt)


def count_users(session: Session) -> int:
    """Return how many users exist."""
    return session.scalar(select(func.count(User.id))) or 0


def list_users(session: Session) -> list[User]:
    """Return every user, ordered by username (case-insensitive)."""
    return list(session.scalars(select(User).order_by(func.lower(User.username), User.id)))


def create_user(
    session: Session,
    username: str,
    password_hash: str,
    roles: Iterable[str] = (),
    *,
    is_active: bool = True,
) -> User:
    """Insert a user and return it.

    Args:
        session: a write session owned by the caller.
        username: the username; its case is kept for display.
        password_hash: an argon2 hash, never a password.
        roles: validated role strings.
        is_active: whether the user may log in.

    Raises:
        UserExistsError: the username is taken (ignoring case), including a
            concurrent insert that won the race. The session stays usable.
    """
    user = User(
        username=username,
        password_hash=password_hash,
        roles=list(dict.fromkeys(roles)),
        is_active=is_active,
    )
    try:
        with session.begin_nested():
            session.add(user)
            session.flush()
    except IntegrityError:
        raise UserExistsError(f"user {username!r} already exists") from None
    return user


def touch_last_login(session: Session, user: User) -> None:
    """Record a successful login now."""
    user.last_login_at = utcnow()
    session.flush()


def set_password_hash(session: Session, user: User, password_hash: str) -> None:
    """Set the password hash and ``credentials_changed_at``, which invalidates older tokens."""
    now = utcnow()
    user.password_hash = password_hash
    user.credentials_changed_at = now
    user.updated_at = now
    session.flush()


def set_roles(session: Session, user: User, roles: Iterable[str]) -> None:
    """Replace the user's roles (validated by the caller); effective on the next request."""
    user.roles = list(dict.fromkeys(roles))
    user.updated_at = utcnow()
    session.flush()


def set_active(session: Session, user: User, is_active: bool) -> None:
    """Enable or disable the user; a disabled user's tokens are rejected at once."""
    user.is_active = is_active
    user.updated_at = utcnow()
    session.flush()


def _is_active_admin(user: User) -> bool:
    return user.is_active and ROLE_ADMIN in user.roles


def _lock_users(session: Session) -> list[User]:
    """Lock and return every user row, in id order.

    The last-admin guard must see a stable set of admins: on Postgres
    ``SELECT ... FOR UPDATE`` makes a concurrent change wait and then re-read
    the committed rows (the order avoids deadlocks); on SQLite the write
    transaction already holds the database write lock and the clause is
    ignored. Call this before loading any user into the session.
    """
    stmt = (
        select(User).order_by(User.id).with_for_update().execution_options(populate_existing=True)
    )
    return list(session.scalars(stmt))


def _find_locked(users: list[User], username: str) -> User:
    wanted = username.lower()
    for user in users:
        if user.username.lower() == wanted:
            return user
    raise UserNotFoundError(f"user {username!r} not found")


def update_user(
    session: Session,
    username: str,
    *,
    password_hash: str | None = None,
    roles: Iterable[str] | None = None,
    is_active: bool | None = None,
) -> User:
    """Change a user; omitted (None) fields stay as they are.

    Runs under the user-table row locks, so the last-admin check and the
    change are atomic with respect to concurrent changes. A new password hash
    sets ``credentials_changed_at`` (older tokens die); roles and ``is_active``
    apply from the next request.

    Args:
        session: a write session owned by the caller (``ctx.write_session()``).
        username: the user, case-insensitive.
        password_hash: a new argon2 hash, never a password.
        roles: the new, validated roles (replacing the old).
        is_active: enable or disable the user.

    Returns:
        The updated user.

    Raises:
        UserNotFoundError: there is no such user.
        LastAdminError: the change would leave no active ``hp-admin``.
    """
    users = _lock_users(session)
    user = _find_locked(users, username)
    new_roles = user.roles if roles is None else list(dict.fromkeys(roles))
    new_active = user.is_active if is_active is None else is_active
    stays_admin = new_active and ROLE_ADMIN in new_roles
    if (
        _is_active_admin(user)
        and not stays_admin
        and not any(_is_active_admin(u) for u in users if u is not user)
    ):
        raise LastAdminError("cannot remove the last active hp-admin")
    now = utcnow()
    if password_hash is not None:
        user.password_hash = password_hash
        user.credentials_changed_at = now
    user.roles = list(new_roles)
    user.is_active = new_active
    user.updated_at = now
    session.flush()
    return user


def delete_user(session: Session, username: str) -> None:
    """Delete a user, atomically with the last-admin check.

    Raises:
        UserNotFoundError: there is no such user.
        LastAdminError: the user is the last active ``hp-admin``.
    """
    users = _lock_users(session)
    user = _find_locked(users, username)
    if _is_active_admin(user) and not any(_is_active_admin(u) for u in users if u is not user):
        raise LastAdminError("cannot remove the last active hp-admin")
    session.delete(user)
    session.flush()

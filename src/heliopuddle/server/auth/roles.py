"""Realm roles: the grammar and the authorization rule (AUTH-07, AUTH-08).

Pure functions with no I/O, shared by the API, the client and the UI. See
docs/design/auth.md.

A role is ``hp-admin`` (global only) or one of the project roles
(``hp-reader``, ``hp-writer``, ``hp-deleter``, ``hp-operator``,
``hp-client-storage-read-write``), optionally scoped to a project as
``<role>:<project>``. A role without a suffix is global.
"""

from __future__ import annotations

from collections.abc import Iterable

ROLE_ADMIN = "hp-admin"
ROLE_READER = "hp-reader"
ROLE_WRITER = "hp-writer"
ROLE_DELETER = "hp-deleter"
ROLE_OPERATOR = "hp-operator"
ROLE_CLIENT_STORAGE_READ_WRITE = "hp-client-storage-read-write"

PROJECT_ROLES = (
    ROLE_READER,
    ROLE_WRITER,
    ROLE_DELETER,
    ROLE_OPERATOR,
    ROLE_CLIENT_STORAGE_READ_WRITE,
)
"""Roles that may be scoped to a project."""

ALL_ROLES = (ROLE_ADMIN, *PROJECT_ROLES)

SENSITIVE_ROLES = frozenset({ROLE_DELETER, ROLE_ADMIN, ROLE_CLIENT_STORAGE_READ_WRITE})
"""Routes needing one of these are "sensitive" for token introspection (AUTH-10)."""


def parse_role(role: str) -> tuple[str, str | None] | None:
    """Split a role string into ``(base role, project or None)``.

    Returns:
        The parts, or None if ``role`` is not a valid hp role (an unknown base
        role, an empty or whitespace-containing project, or ``hp-admin`` with
        a project).
    """
    base, sep, project = role.partition(":")
    if base == ROLE_ADMIN:
        return (ROLE_ADMIN, None) if not sep else None
    if base not in PROJECT_ROLES:
        return None
    if not sep:
        return (base, None)
    if not project or project != project.strip() or any(c.isspace() for c in project):
        return None
    return (base, project)


def validate_roles(roles: Iterable[str]) -> list[str]:
    """Return ``roles`` without duplicates (order kept), rejecting invalid ones.

    Raises:
        ValueError: naming every invalid role string.
    """
    seen: dict[str, None] = {}
    bad: list[str] = []
    for role in roles:
        if parse_role(role) is None:
            bad.append(role)
        else:
            seen.setdefault(role)
    if bad:
        raise ValueError(
            f"invalid role(s): {', '.join(repr(b) for b in bad)}; use {ROLE_ADMIN} or "
            f"{', '.join(PROJECT_ROLES)} with an optional ':<project>' suffix"
        )
    return list(seen)


def valid_roles(roles: Iterable[str]) -> list[str]:
    """Return the valid hp roles in ``roles`` (others, such as ``offline_access``, dropped)."""
    return list(dict.fromkeys(r for r in roles if parse_role(r) is not None))


def allowed(roles: Iterable[str], needed: str, project: str | None = None) -> bool:
    """Whether ``roles`` grant ``needed`` (on ``project``, for a project route).

    ``hp-admin`` grants everything. A global project role grants itself on every
    project, and ``<role>:<project>`` only on that project. Every project role
    also grants read, but implies no other roles. With ``project=None`` only
    global roles count.

    Args:
        roles: the caller's role strings (invalid ones are ignored).
        needed: ``hp-admin`` or one of :data:`PROJECT_ROLES`.
        project: the project the route is about, or None for a project-agnostic route.
    """
    for role in roles:
        parsed = parse_role(role)
        if parsed is None:
            continue
        base, scope = parsed
        if base == ROLE_ADMIN:
            return True
        if needed == ROLE_ADMIN:
            continue
        if scope is not None and scope != project:
            continue
        if base == needed or needed == ROLE_READER:
            return True
    return False


def has_any_role_on(roles: Iterable[str], project: str) -> bool:
    """Whether ``roles`` include any role that applies to ``project`` (404 vs 403, AUTH-08)."""
    return allowed(roles, ROLE_READER, project)


def is_sensitive(needed: str) -> bool:
    """Whether a route needing ``needed`` is "sensitive" for introspection (AUTH-10)."""
    return needed in SENSITIVE_ROLES

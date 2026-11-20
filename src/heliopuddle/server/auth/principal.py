"""The authenticated caller."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from heliopuddle.server.auth.roles import ROLE_ADMIN, allowed, has_any_role_on

AuthMode = Literal["local", "oidc", "header"]


@dataclass(frozen=True)
class Principal:
    """Who is calling and what they may do.

    Attributes:
        username: the authenticated username; in ``header`` mode the header
            value (a request without it is refused before this, AUTH-20).
        roles: valid hp role strings. In ``header`` mode no roles are enforced
            (AUTH-01), so the principal carries ``hp-admin`` and every check passes.
        mode: the auth mode that produced it.
        token: the bearer token (None in ``header`` mode); used for introspection.
    """

    username: str | None
    roles: tuple[str, ...]
    mode: AuthMode
    token: str | None = None

    def can(self, needed: str, project: str | None = None) -> bool:
        """Whether the caller holds ``needed`` (on ``project``); see :func:`roles.allowed`."""
        return allowed(self.roles, needed, project)

    def has_any_role_on(self, project: str) -> bool:
        """Whether any of the caller's roles applies to ``project``."""
        return has_any_role_on(self.roles, project)


def header_principal(username: str) -> Principal:
    """The principal for ``header`` mode: a trusted proxy vouches for the user (AUTH-01)."""
    return Principal(username=username, roles=(ROLE_ADMIN,), mode="header")

"""User management calls (AUTH-13; ``local`` mode, ``hp-admin``)."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from heliopuddle.client._base import ClientBase
from heliopuddle.server.schemas import UserRead


class UsersMixin(ClientBase):
    """``list_users`` .. ``delete_user`` of :class:`HelioPuddleClient`."""

    def list_users(self) -> list[UserRead]:
        """Return ``GET /users``."""
        return [UserRead.model_validate(u) for u in self._request("GET", "/users").json()]

    def get_user(self, username: str) -> UserRead:
        """Return ``GET /users/{username}``.

        Raises:
            NotFoundError: no such user (or the server is not in ``local`` mode).
        """
        return UserRead.model_validate(self._request("GET", user_url(username)).json())

    def create_user(
        self,
        username: str,
        password: str,
        roles: list[str] | None = None,
        is_active: bool = True,
    ) -> UserRead:
        """Create a user (``POST /users``).

        Raises:
            HelioPuddleError: ``409`` for a duplicate username, ``422`` for a
                short password or an invalid role.
        """
        body: dict[str, Any] = {"username": username, "password": password, "is_active": is_active}
        if roles is not None:
            body["roles"] = roles
        return UserRead.model_validate(self._request("POST", "/users", json=body).json())

    def update_user(
        self,
        username: str,
        *,
        password: str | None = None,
        roles: list[str] | None = None,
        is_active: bool | None = None,
    ) -> UserRead:
        """Change a user (``PATCH /users/{username}``); omitted fields stay as they are.

        Raises:
            HelioPuddleError: ``409`` if it would remove the last active
                ``hp-admin``; ``422`` for a short password or invalid role.
        """
        body: dict[str, Any] = {}
        if password is not None:
            body["password"] = password
        if roles is not None:
            body["roles"] = roles
        if is_active is not None:
            body["is_active"] = is_active
        response = self._request("PATCH", user_url(username), json=body)
        return UserRead.model_validate(response.json())

    def delete_user(self, username: str) -> None:
        """Delete a user (``DELETE /users/{username}``).

        Raises:
            HelioPuddleError: ``409`` for the last active ``hp-admin``.
        """
        self._request("DELETE", user_url(username))


def user_url(username: str) -> str:
    return f"/users/{quote(username, safe='')}"

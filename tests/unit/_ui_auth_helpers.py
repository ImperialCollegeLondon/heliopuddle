"""Shared stub, factory and fixtures for the UI login and session tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient

from heliopuddle.client import (
    AuthenticationError,
    LoginResult,
    Me,
)
from heliopuddle.server.ui import create_ui
from heliopuddle.settings import Settings
from tests.unit._ui_helpers import (
    Factory,
    StubClient,
    series_files,
)

SECRET = "x" * 40


PASSWORD = "correct horse battery"


@dataclass
class AuthStub(StubClient):
    """A stub client that can log in and answer ``/auth/me`` for the factory's users."""

    backend: AuthFactory | None = None
    creds: tuple[str, str] | None = None

    def _maybe_raise(self) -> None:
        assert self.backend is not None
        if self.token is not None and self.token in self.backend.revoked:
            raise AuthenticationError(401, "token expired")
        super()._maybe_raise()

    def login(self) -> LoginResult:
        assert self.backend is not None and self.creds is not None
        if self.backend.login_error is not None:
            raise self.backend.login_error
        username, password = self.creds
        known = self.backend.users.get(username)
        if known is None or known[0] != password:
            raise AuthenticationError(400, "token request refused: invalid_grant")
        token = f"tok-{username}-{len(self.backend.identities)}"
        self.backend.identities[token] = (username, known[1])
        self.token = token
        expires = datetime.now(UTC) + timedelta(seconds=self.backend.ttl)
        return LoginResult(token, expires)

    def me(self) -> Me:
        assert self.backend is not None
        self._maybe_raise()
        who = self.backend.identities.get(self.token or "")
        if who is None:
            raise AuthenticationError(401, "unknown token")
        return Me(username=who[0], roles=list(who[1]), mode="local")


@dataclass
class AuthFactory(Factory):
    """A client factory for ``local`` and ``oidc`` mode."""

    users: dict[str, tuple[str, list[str]]] = field(
        default_factory=lambda: {"ann": (PASSWORD, ["hp-writer"])}
    )
    identities: dict[str, tuple[str, list[str]]] = field(default_factory=dict)
    revoked: set[str] = field(default_factory=set)
    ttl: float = 3600
    login_error: Exception | None = None

    def __call__(  # type: ignore[override]
        self,
        project: str | None,
        user: str | None = None,
        *,
        token: str | None = None,
        username: str | None = None,
        password: str | None = None,
    ) -> Any:
        client = AuthStub(project, user, list(self.projects), self.files, self.error)
        client.backend = self
        client.token = token
        if username is not None and password is not None:
            client.creds = (username, password)
        client.storage_locations = self.storage_locations or client.storage_locations
        client.action_error = self.action_error
        client.rules = self.rules
        self.made.append(client)
        return client

    def api_calls(self) -> list[str]:
        """The names of the calls made through tokens (not logins)."""
        return [name for c in self.made if c.token for name, _ in c.calls]


def make_settings(**values: Any) -> Settings:
    """``Settings`` from plain values (strings for the secret fields)."""
    return Settings(**values)


def ui(factory: AuthFactory, **settings: Any) -> TestClient:
    values = {"auth_mode": "local", "auth_secret_key": SECRET, **settings}
    app = create_ui(Settings(**values), client_factory=factory)
    return TestClient(app, raise_server_exceptions=False)


def login(
    browser: TestClient, username: str = "ann", password: str = PASSWORD, next: str = "/", **kw: Any
) -> Any:
    return browser.post(
        "/login",
        data={"username": username, "password": password, "next": next},
        follow_redirects=False,
        **kw,
    )


def location(response: Any) -> str:
    assert response.status_code == 303, response.text
    return str(response.headers["location"])


def with_roles(*roles: str) -> tuple[AuthFactory, TestClient]:
    factory = AuthFactory(
        users={"ann": (PASSWORD, list(roles))}, files=series_files(), projects=["demo"]
    )
    browser = ui(factory)
    assert location(login(browser)) == "/"
    return factory, browser

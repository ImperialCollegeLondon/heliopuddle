"""Shared fake API and identity provider for the client auth tests."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs

import httpx

from heliopuddle.client import (
    HelioPuddleClient,
)

API = "http://api.test/api/v1"


IDP = "http://idp.test/token"


class Fake:
    """A fake API (with ``/auth/*``) and an OIDC token endpoint."""

    def __init__(
        self,
        mode: str = "local",
        *,
        expires_in: int = 3600,
        token_url: str | None = f"{API}/auth/token",
        accepted: set[str] | None = None,
    ) -> None:
        self.mode = mode
        self.expires_in = expires_in
        self.token_url = token_url
        self.issued = 0
        self.accepted = accepted
        self.accept_issued = True
        self.requests: list[httpx.Request] = []
        self.token_requests: list[httpx.Request] = []
        self.config_calls = 0
        self.token_error: tuple[int, dict[str, Any]] | None = None
        self.api_status: dict[str, tuple[int, Any]] = {}
        self.users: dict[str, dict[str, Any]] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url == IDP or url == f"{API}/auth/token":
            return self._token(request)
        if request.url.path == "/api/v1/auth/config":
            self.config_calls += 1
            body: dict[str, Any] = {"mode": self.mode, "token_url": self.token_url}
            return httpx.Response(200, json=body)
        auth = request.headers.get("authorization")
        if (
            self.mode != "header"
            and self.accepted is not None
            and (auth is None or auth.removeprefix("Bearer ") not in self.accepted)
        ):
            return httpx.Response(401, json={"detail": "invalid credentials"})
        path = request.url.path.removeprefix("/api/v1")
        if path in self.api_status:
            status, body = self.api_status[path]
            return httpx.Response(status, json=body)
        if path == "/auth/me":
            return httpx.Response(
                200, json={"username": "alice", "roles": ["hp-reader"], "mode": self.mode}
            )
        return httpx.Response(200, json=[])

    def _token(self, request: httpx.Request) -> httpx.Response:
        self.token_requests.append(request)
        if self.token_error is not None:
            status, body = self.token_error
            return httpx.Response(status, json=body)
        self.issued += 1
        token = f"tok{self.issued}"
        if self.accepted is not None and self.accept_issued:
            self.accepted.add(token)
        return httpx.Response(
            200, json={"access_token": token, "token_type": "bearer", "expires_in": self.expires_in}
        )

    def client(self, **kwargs: Any) -> HelioPuddleClient:
        http = httpx.Client(base_url=API, transport=httpx.MockTransport(self.handler))
        return HelioPuddleClient(API, "demo", http_client=http, **kwargs)


def form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


def bearer(request: httpx.Request) -> str | None:
    return request.headers.get("authorization")


def _user(name: str, **kw: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": 1,
        "username": name,
        "roles": [],
        "is_active": True,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "last_login_at": None,
    }
    row.update(kw)
    return row


def _api(handler: Any) -> HelioPuddleClient:
    http = httpx.Client(base_url=API, transport=httpx.MockTransport(handler))
    return HelioPuddleClient(API, "demo", token="t", http_client=http)


SECURE_API = "https://api.example/api/v1"


def secure_client(
    token_url: str | None, mode: str = "local", **creds: Any
) -> tuple[HelioPuddleClient, list[httpx.Request]]:
    """A client for an https API whose /auth/config announces ``token_url``."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/v1/auth/config":
            return httpx.Response(200, json={"mode": mode, "token_url": token_url})
        if request.url.path.endswith("/token"):
            return httpx.Response(
                200, json={"access_token": "tok", "token_type": "bearer", "expires_in": 3600}
            )
        return httpx.Response(200, json={"username": "alice", "roles": ["hp-reader"], "mode": mode})

    http = httpx.Client(base_url=SECURE_API, transport=httpx.MockTransport(handler))
    creds = creds or {"username": "alice", "password": "a-long-enough-password"}
    return HelioPuddleClient(SECURE_API, "demo", http_client=http, **creds), seen

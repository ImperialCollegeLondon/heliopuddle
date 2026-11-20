"""Token acquisition and caching for :class:`heliopuddle.client.HelioPuddleClient` (AUTH-15).

An :class:`AuthSession` turns the caller's credentials into request headers.
Nothing is fetched until the first request. Then the session asks the public
``GET /auth/config`` which mode the server runs in and where tokens come from:

* ``local``: ``POST`` the OAuth2 password form (``grant_type=password``,
  ``username``, ``password``) to the API's own ``/auth/token``, on the origin
  the client was configured with. The ``token_url`` the server announces is
  ignored in this mode: behind a TLS-terminating proxy the server may not know
  its public scheme and announce ``http://``, and a password must never follow
  such a URL.
* ``oidc``: ``POST`` to the provider's ``token_url``. Username and password use
  the ``password`` grant (a direct access grant); client id and secret alone
  use the ``client_credentials`` grant. A client secret is sent with HTTP basic
  authentication (``client_secret_basic``); a client id without a secret goes
  in the form (a public client).
* ``header``: no token. The ``user`` header is sent instead.

A password or secret is never sent over plain ``http://`` when the client's API
URL is ``https://`` (an ``oidc`` token endpoint is refused in that case).

The token is cached with its expiry and renewed under a lock, so threads that
share a client fetch one token. A caller-supplied ``token`` is used as is and
cannot be renewed. Passwords, secrets and tokens never appear in ``repr`` or
in error messages.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from heliopuddle.client.errors import AuthenticationError
from heliopuddle.server.schemas.auth import AuthConfig

TOKEN_SKEW = 30.0
"""Seconds before the stated expiry at which a cached token is treated as expired."""

DEFAULT_EXPIRES_IN = 300
"""Lifetime assumed when a provider omits ``expires_in``."""


def _monotonic() -> float:
    return time.monotonic()


@dataclass(frozen=True, repr=False)
class Credentials:
    """What the caller supplied; at most one kind (see :meth:`validate`)."""

    token: str | None = None
    username: str | None = None
    password: str | None = None
    client_id: str | None = None
    client_secret: str | None = None

    def __repr__(self) -> str:
        return f"Credentials(kind={self.kind!r})"

    @property
    def kind(self) -> str:
        """``token``, ``password``, ``client`` or ``none``."""
        if self.token is not None:
            return "token"
        if self.username is not None:
            return "password"
        if self.client_id is not None:
            return "client"
        return "none"

    def validate(self) -> None:
        """Reject incomplete or conflicting credentials.

        ``client_id`` (and ``client_secret``) may accompany a username and
        password: they identify the OIDC client for the password grant.

        Raises:
            ValueError: more than one credential kind, a username without a
                password (or the reverse), a ``client_secret`` without a
                ``client_id``, or a ``client_id`` with neither a secret nor a
                username and password.
        """
        if (self.username is None) != (self.password is None):
            raise ValueError("username and password must be given together")
        if self.client_secret is not None and self.client_id is None:
            raise ValueError("client_secret needs a client_id")
        if self.token is not None and any(
            v is not None for v in (self.username, self.client_id, self.client_secret)
        ):
            raise ValueError("give only one of token, username/password or client_id/client_secret")
        if self.username is None and self.client_id is not None and self.client_secret is None:
            raise ValueError("client_id needs a client_secret (or a username and password)")


@dataclass(frozen=True)
class LoginResult:
    """A token obtained by :meth:`~heliopuddle.client.HelioPuddleClient.login`.

    Attributes:
        access_token: the bearer token. It is left out of ``repr``.
        expires_at: when the token expires (UTC), or None if that is not known
            (a token the caller supplied).
    """

    access_token: str = field(repr=False)
    expires_at: datetime | None = None


class AuthSession:
    """Resolves request headers for one client: token, cache, refresh.

    Args:
        http: the client's HTTP client (base URL = the API).
        credentials: validated credentials.
        user: user id for the ``user`` header.
        user_header: name of that header.
    """

    def __init__(
        self,
        http: httpx.Client,
        credentials: Credentials,
        *,
        user: str | None,
        user_header: str,
    ) -> None:
        credentials.validate()
        self._http = http
        self._credentials = credentials
        self._user = user
        self._user_header = user_header
        self._lock = threading.Lock()
        self._config: AuthConfig | None = None
        self._token: str | None = None
        self._expires_at = 0.0
        self._expires_wall = 0.0

    def __repr__(self) -> str:
        return f"AuthSession(credentials={self._credentials!r})"

    @property
    def can_refresh(self) -> bool:
        """Whether a new token can be fetched (False for a static token or none)."""
        return self._credentials.kind in ("password", "client")

    def headers(self) -> dict[str, str]:
        """Headers for the next request, fetching a token first if needed.

        Raises:
            AuthenticationError: the token request failed.
        """
        kind = self._credentials.kind
        if kind == "none":
            return self._user_headers()
        if kind == "token":
            return {"Authorization": f"Bearer {self._credentials.token}"}
        with self._lock:
            config = self._load_config()
            if config.mode == "header":
                return self._user_headers()
            if self._token is None or _monotonic() >= self._expires_at:
                self._fetch(config)
            return {"Authorization": f"Bearer {self._token}"}

    def login(self) -> LoginResult:
        """Return the current token (fetching one first if needed) and its expiry.

        Raises:
            AuthenticationError: there are no credentials, the server issues no
                tokens (``header`` mode) or the token request failed.
        """
        kind = self._credentials.kind
        if kind == "token":
            assert self._credentials.token is not None
            return LoginResult(self._credentials.token)
        if kind == "none":
            raise AuthenticationError(401, "no credentials to log in with")
        with self._lock:
            config = self._load_config()
            if config.mode == "header":
                raise AuthenticationError(
                    400, "the server is in header mode, which does not issue tokens"
                )
            if self._token is None or _monotonic() >= self._expires_at:
                self._fetch(config)
            assert self._token is not None
            return LoginResult(self._token, datetime.fromtimestamp(self._expires_wall, UTC))

    def invalidate(self, headers: dict[str, str]) -> None:
        """Forget the token that ``headers`` carried (it got a ``401``).

        A token another thread has already replaced is kept.
        """
        sent = headers.get("Authorization", "").removeprefix("Bearer ")
        with self._lock:
            if self._token is not None and self._token == sent:
                self._token = None

    def _user_headers(self) -> dict[str, str]:
        return {self._user_header: self._user} if self._user else {}

    def _load_config(self) -> AuthConfig:
        if self._config is None:
            self._config = self.fetch_config(self._http)
        return self._config

    @staticmethod
    def fetch_config(http: httpx.Client) -> AuthConfig:
        """Return ``GET /auth/config``.

        Raises:
            AuthenticationError: the server does not answer it (an API that
                predates authentication cannot issue tokens).
        """
        try:
            response = http.get("/auth/config")
        except httpx.HTTPError as exc:
            raise AuthenticationError(0, f"cannot reach the API: {exc}") from exc
        if response.status_code != 200:
            raise AuthenticationError(
                response.status_code, "the server's /auth/config is not available"
            )
        return AuthConfig.model_validate(response.json())

    def _require_secure_transport(self, token_url: str) -> None:
        """Refuse to send credentials over ``http://`` from a client talking ``https://``."""
        if self._http.base_url.scheme == "https" and httpx.URL(token_url).scheme != "https":
            raise AuthenticationError(
                401,
                "refusing to send credentials: the API is reached over https but the "
                "announced token endpoint is plain http",
            )

    def _fetch(self, config: AuthConfig) -> None:
        c = self._credentials
        form: dict[str, str]
        auth: httpx.BasicAuth | None = None
        if c.kind == "password":
            assert c.username is not None and c.password is not None
            form = {"grant_type": "password", "username": c.username, "password": c.password}
        else:
            if config.mode == "local":
                raise AuthenticationError(
                    401, "the server is in local mode, which has no client credentials grant"
                )
            form = {"grant_type": "client_credentials"}
        if config.mode == "oidc" and c.client_id is not None:
            if c.client_secret is not None:
                auth = httpx.BasicAuth(c.client_id, c.client_secret)
            else:
                form["client_id"] = c.client_id
        if config.mode == "local":
            url = "/auth/token"  # the client's own API origin, whatever the server announced
        else:
            if config.token_url is None:
                raise AuthenticationError(401, "the server did not announce a token endpoint")
            url = config.token_url
            self._require_secure_transport(url)
        try:
            response = self._http.post(
                url, data=form, auth=auth if auth is not None else httpx.USE_CLIENT_DEFAULT
            )
        except httpx.HTTPError as exc:
            raise AuthenticationError(0, f"cannot reach the token endpoint: {exc}") from exc
        if response.status_code != 200:
            raise AuthenticationError(response.status_code, _token_error(response))
        try:
            body: dict[str, Any] = response.json()
            token = str(body["access_token"])
        except (ValueError, KeyError, TypeError) as exc:
            raise AuthenticationError(
                response.status_code, "the token endpoint answered without an access_token"
            ) from exc
        try:
            expires_in = float(body.get("expires_in", DEFAULT_EXPIRES_IN))
        except (TypeError, ValueError):
            expires_in = float(DEFAULT_EXPIRES_IN)
        skew = TOKEN_SKEW if expires_in > 2 * TOKEN_SKEW else expires_in / 2
        self._token = token
        self._expires_at = _monotonic() + expires_in - skew
        self._expires_wall = time.time() + expires_in


def _token_error(response: httpx.Response) -> str:
    """A message for a failed token request: the OAuth error, never the request."""
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        error = body.get("error")
        description = body.get("error_description") or body.get("detail")
        if isinstance(error, str) and isinstance(description, str):
            return f"token request refused: {error}: {description}"
        if isinstance(error, str):
            return f"token request refused: {error}"
        if isinstance(description, str):
            return f"token request refused: {description}"
    return f"token request failed with status {response.status_code}"

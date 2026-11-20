"""HelioPuddleClient: HTTP to the API, optional direct fsspec access to the store.

Requirements: CLI-01 to CLI-09. The client never imports the database layer;
it talks to the API over HTTP and reads and writes file content straight from
and to the storage location when authorized, otherwise through the API. Uploads
follow docs/design/operations.md: reserve, write, renew while writing, complete.
"""

from __future__ import annotations

import httpx

from heliopuddle.client._base import ClientBase
from heliopuddle.client._content import ContentMixin
from heliopuddle.client._rules import RulesMixin
from heliopuddle.client._uploads import UploadsMixin
from heliopuddle.client._users import UsersMixin
from heliopuddle.client.auth import AuthSession, Credentials
from heliopuddle.client.errors import (
    AuthenticationError,
    BusyError,
    ConflictError,
    HelioPuddleError,
    IntegrityError,
    NotFoundError,
    PermissionDeniedError,
)
from heliopuddle.client.paging import Order

__all__ = [
    "AuthenticationError",
    "BusyError",
    "ConflictError",
    "HelioPuddleClient",
    "HelioPuddleError",
    "IntegrityError",
    "NotFoundError",
    "Order",
    "PermissionDeniedError",
]

DEFAULT_TIMEOUT = 300.0


class HelioPuddleClient(UsersMixin, RulesMixin, UploadsMixin, ContentMixin, ClientBase):
    """Client bound to one project.

    Talks to the API over HTTP only and writes file content to the store with
    fsspec. Use it as a context manager, or call :meth:`close` when done.

    Args:
        api_url: base URL of the API, including ``/api/v1``
            (e.g. ``http://host:8080/api/v1``). Ignored when ``http_client`` is given.
        project: project name. May be None for project-agnostic calls
            (:meth:`health`, :meth:`list_projects`); project-scoped methods then
            raise ``ValueError``.
        user: user id sent in the ``user_header`` header. It is sent when no
            credentials are given, and when credentials are given but the
            server runs in ``header`` mode; never alongside a token.
        user_header: header name; must match the server's ``HELIOPUDDLE_USER_HEADER``.
        http_client: optional pre-built ``httpx.Client`` whose base URL points
            at the API (tests pass ``fastapi.testclient.TestClient``). The
            client sends the user header per request and does not close a
            client it did not create.
        timeout: request timeout in seconds, for the client built here.
        token: a ready-made bearer token, used as is (it cannot be renewed).
        username: user name for a token request; needs ``password``.
        password: the user's password.
        client_id: OIDC client id. With ``client_secret`` and no username it
            requests a token with the ``client_credentials`` grant (a service
            account, which needs realm roles); with a username and password it
            identifies the client for the ``password`` grant.
        client_secret: the client's secret (sent with HTTP basic authentication).

    At most one credential kind is allowed: ``token``, ``username`` with
    ``password``, or ``client_id`` with ``client_secret``. A token is fetched
    lazily on the first request (see :mod:`heliopuddle.client.auth`), cached
    until shortly before it expires, and renewed once after a ``401``.

    Raises:
        ValueError: conflicting or incomplete credentials.
    """

    def __init__(
        self,
        api_url: str,
        project: str | None,
        user: str | None = None,
        *,
        user_header: str = "X-Forwarded-User",
        http_client: httpx.Client | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        token: str | None = None,
        username: str | None = None,
        password: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
    ) -> None:
        credentials = Credentials(token, username, password, client_id, client_secret)
        credentials.validate()
        self.api_url = api_url
        self.project = project
        self.user = user
        self.user_header = user_header
        self._owns_http = http_client is None
        self._http = (
            http_client
            if http_client is not None
            else httpx.Client(base_url=api_url.rstrip("/"), timeout=timeout)
        )
        self._auth = AuthSession(self._http, credentials, user=user, user_header=user_header)

    def __repr__(self) -> str:
        return f"HelioPuddleClient(api_url={self.api_url!r}, project={self.project!r})"

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        """Close the HTTP connection pool (only if this client created it)."""
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> HelioPuddleClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

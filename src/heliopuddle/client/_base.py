"""Plumbing and project-agnostic calls shared by every client mixin."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from typing import Any
from urllib.parse import quote

import httpx

from heliopuddle.client._http import api_error
from heliopuddle.client.auth import AuthSession, LoginResult
from heliopuddle.client.errors import NotFoundError, PermissionDeniedError
from heliopuddle.server.schemas import (
    AuthConfig,
    HealthResponse,
    Me,
    ProjectRead,
    ProjectSummary,
    SearchResults,
    StorageLocationDetail,
    StorageLocationRead,
)


class ClientBase:
    """State and request plumbing of :class:`HelioPuddleClient`; see its docstring."""

    api_url: str
    project: str | None
    user: str | None
    user_header: str
    _owns_http: bool
    _http: httpx.Client
    _auth: AuthSession

    def _project(self) -> str:
        if self.project is None:
            raise ValueError("this call needs a project; create the client with project=...")
        return quote(self.project, safe="")

    def _request(
        self,
        method: str,
        url: str,
        *,
        accept: tuple[int, ...] = (),
        stream: bool = False,
        body_factory: Callable[[], Iterator[bytes]] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Send a request; raise for error statuses other than those in ``accept``."""
        extra_headers = kwargs.pop("headers", {})
        headers = {**extra_headers, **self._auth.headers()}

        def send() -> httpx.Response:
            if body_factory is not None:
                kwargs["content"] = body_factory()
            if stream:
                return self._http.send(
                    self._http.build_request(method, url, headers=headers, **kwargs), stream=True
                )
            return self._http.request(method, url, headers=headers, **kwargs)

        response = send()
        if response.status_code == 401 and self._auth.can_refresh:
            response.close()
            self._auth.invalidate(headers)
            headers = {**extra_headers, **self._auth.headers()}
            response = send()
        if response.status_code >= 400 and response.status_code not in accept:
            try:
                response.read()
                raise api_error(response)
            finally:
                response.close()
        return response

    # -- project-agnostic ----------------------------------------------------

    def health(self) -> HealthResponse:
        """Return ``GET /health``.

        A ``503`` (unhealthy) is returned as a response with ``status="error"``,
        not raised.
        """
        response = self._request("GET", "/health", accept=(503,))
        return HealthResponse.model_validate(response.json())

    def list_projects(self) -> list[ProjectRead]:
        """Return ``GET /projects``."""
        return [ProjectRead.model_validate(p) for p in self._request("GET", "/projects").json()]

    def search(
        self,
        q: str,
        projects: Sequence[str] | None = None,
        *,
        latest_only: bool = False,
        include_deleted: bool = False,
        limit: int = 20,
        offset: int = 0,
    ) -> SearchResults:
        """Search file paths across projects (``GET /search/files``).

        Not scoped to the client's project: every project the caller holds a
        role on is searched unless ``projects`` narrows them.

        Args:
            q: words separated by spaces (2 to 200 characters); each must occur
                in the path, case-insensitively, as literal text.
            projects: search only these projects; each must be readable.
            latest_only: only the latest version of each series.
            include_deleted: also return deleted files.
            limit: page size, 1 to 100.
            offset: number of ranked matches to skip.

        Returns:
            The ranked page, the total and the matches per project.

        Raises:
            NotFoundError: if a named project is unknown or not readable.
            HelioPuddleError: for other error statuses (``422`` for bad parameters).
        """
        params: dict[str, Any] = {
            "q": q,
            "latest_only": latest_only,
            "include_deleted": include_deleted,
            "limit": limit,
            "offset": offset,
        }
        if projects:
            params["project"] = list(projects)
        response = self._request("GET", "/search/files", params=params)
        return SearchResults.model_validate(response.json())

    def project_summary(self, project: str | None = None) -> ProjectSummary:
        """Return ``GET /projects/{p}/summary``: file, copy and view counts.

        Args:
            project: the project; defaults to the client's project.

        Returns:
            The project's summary.

        Raises:
            NotFoundError: if the project is unknown or not readable.
        """
        name = self._project() if project is None else quote(project, safe="")
        response = self._request("GET", f"/projects/{name}/summary")
        return ProjectSummary.model_validate(response.json())

    # -- authentication ------------------------------------------------------

    def auth_config(self) -> AuthConfig:
        """Return ``GET /auth/config`` (public): the server's auth mode and token URL."""
        response = self._http.get("/auth/config")  # public: no credentials, no token fetch
        if response.status_code >= 400:
            raise api_error(response)
        return AuthConfig.model_validate(response.json())

    def login(self) -> LoginResult:
        """Get the token for this client's credentials, fetching it if needed.

        For a client built with ``username`` and ``password`` (or a client id and
        secret) this requests a token, or returns the cached one while it is
        valid. A client built with ``token=`` returns that token. The token stays
        out of ``repr``.

        Returns:
            The access token and when it expires.

        Raises:
            AuthenticationError: no credentials, the credentials were refused,
                or the server issues no tokens (``header`` mode).
        """
        return self._auth.login()

    def me(self) -> Me:
        """Return ``GET /auth/me``: the caller's username, roles and the server's mode."""
        return Me.model_validate(self._request("GET", "/auth/me").json())

    def change_password(self, current: str, new: str) -> None:
        """Change the caller's own password (``POST /auth/me/password``, ``local`` mode).

        Args:
            current: the current password.
            new: the new password (the server enforces its minimum length).

        Raises:
            HelioPuddleError: the server refused (``403`` for a wrong current
                password, ``422`` for a short new one).
        """
        body = {"current_password": current, "new_password": new}
        self._request("POST", "/auth/me/password", json=body)

    # -- storage locations ---------------------------------------------------

    def list_storage_locations(self) -> list[StorageLocationRead]:
        """Return ``GET /projects/{p}/storage-locations``, including inactive ones."""
        rows = self._request("GET", f"/projects/{self._project()}/storage-locations").json()
        return [StorageLocationRead.model_validate(r) for r in rows]

    def get_storage_location(self, name: str | None = None) -> StorageLocationDetail:
        """Return a storage location with its resolved storage options.

        Args:
            name: storage location name. None means the project's first
                (active) storage location, the same default the API uses.

        Raises:
            NotFoundError: unknown project or storage location, or the project
                has no active storage location.
            PermissionDeniedError: missing ``hp-client-storage-read-write`` on
                this project (or ``hp-admin``). File transfers use the API instead.
        """
        project = self._project()
        if name is None:
            info = ProjectRead.model_validate(self._request("GET", f"/projects/{project}").json())
            if not info.storage_locations:
                raise NotFoundError(404, f"project {self.project!r} has no storage location")
            name = info.storage_locations[0]
        row = self._request(
            "GET", f"/projects/{project}/storage-locations/{quote(name, safe='')}"
        ).json()
        return StorageLocationDetail.model_validate(row)

    def _transfer_location(self, name: str | None = None) -> StorageLocationDetail | None:
        """The storage location for direct access, or None to transfer through the API.

        An incomplete storage location (a Prefect block is missing, STO-18) goes
        through the API too, which answers ``409 storage_location_incomplete``,
        and so does one only the server reaches (``direct_access: false``, rclone).
        """
        try:
            location = self.get_storage_location(name)
        except PermissionDeniedError as error:
            if "hp-client-storage-read-write" not in error.required_roles:
                raise
            return None
        if location.status == "incomplete" or not location.direct_access:
            return None
        return location

"""How the UI gets a client for the API: the factory protocol and the default factory."""

from __future__ import annotations

from typing import Protocol

from heliopuddle.client import HelioPuddleClient
from heliopuddle.settings import Settings


class ClientFactory(Protocol):
    """Builds a client for one UI request.

    Called as ``factory(project, user=..., token=...)`` to talk to the API, and as
    ``factory(None, username=..., password=...)`` to log a local user in.
    """

    def __call__(
        self,
        project: str | None,
        user: str | None = None,
        *,
        token: str | None = None,
        username: str | None = None,
        password: str | None = None,
    ) -> HelioPuddleClient:
        """Build a client; ``project`` is None for the picker and for logins."""
        ...


def default_api_url(settings: Settings) -> str:
    """The API base URL the UI calls.

    Args:
        settings: process settings.

    Returns:
        ``settings.ui_api_url`` if set; otherwise the API in this process over
        loopback, ``http://127.0.0.1:{port}{api_root_path}/api/v1``. No
        trailing slash.
    """
    if settings.ui_api_url:
        return settings.ui_api_url.rstrip("/")
    root = settings.api_root_path.rstrip("/")
    return f"http://127.0.0.1:{settings.port}{root}/api/v1"


def default_client_factory(settings: Settings) -> ClientFactory:
    """The factory used when none is given: clients for :func:`default_api_url`.

    Args:
        settings: process settings (``user_header`` names the header forwarded in
            ``header`` mode).

    Returns:
        A factory building a :class:`~heliopuddle.client.HelioPuddleClient`.
    """
    api_url = default_api_url(settings)

    def factory(
        project: str | None,
        user: str | None = None,
        *,
        token: str | None = None,
        username: str | None = None,
        password: str | None = None,
    ) -> HelioPuddleClient:
        return HelioPuddleClient(
            api_url,
            project,
            user=user,
            user_header=settings.user_header,
            token=token,
            username=username,
            password=password,
        )

    return factory

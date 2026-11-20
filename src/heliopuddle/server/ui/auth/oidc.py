"""Helpers of the UI's OpenID Connect login: PKCE, absolute URLs and the provider cache."""

from __future__ import annotations

import base64
import hashlib
import threading
from collections.abc import Callable

from fastapi import Request

from heliopuddle.server.auth.oidc import OidcProvider
from heliopuddle.server.ui.auth.session import forwarded


def pkce_challenge(verifier: str) -> str:
    """The S256 PKCE challenge of a code verifier."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def external_url(request: Request, name: str) -> str:
    """The absolute URL of a UI route as the browser sees it (behind a proxy too)."""
    url = request.url_for(name)
    proto = forwarded(request.headers, "x-forwarded-proto")
    if proto in ("http", "https"):
        url = url.replace(scheme=proto)
    host = str(request.headers.get("x-forwarded-host", "")).split(",")[0].strip()
    if host:
        url = url.replace(netloc=host)
    return str(url)


class OidcProviderCache:
    """Builds the OIDC provider on first use and keeps it (a UI-only process has no context).

    Args:
        make: builds the provider, for example ``OidcProvider.discover(settings)``.
    """

    def __init__(self, make: Callable[[], OidcProvider]) -> None:
        self._make = make
        self._provider: OidcProvider | None = None
        self._lock = threading.Lock()

    def get(self) -> OidcProvider:
        """Return the provider, discovering it the first time.

        Raises:
            OidcStartupError: the provider cannot be reached. Nothing is cached, so
                the next call tries again.
        """
        with self._lock:
            if self._provider is None:
                self._provider = self._make()
            return self._provider

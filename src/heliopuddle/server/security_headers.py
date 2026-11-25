"""Security response headers for the API and the UI (AUTH-19).

A small ASGI middleware: it adds the headers below to every response and never
replaces one a route already set. A route's own ``Content-Security-Policy`` is
kept, with the ``frame-ancestors`` directive appended when it has none. Who may
frame hp pages is a setting (``HELIOPUDDLE_FRAME_ANCESTORS``, default
``'none'``), for a portal that embeds the UI. It imports nothing from the
database layer, so the UI may use it.
"""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

DEFAULT_FRAME_ANCESTORS = "'none'"

DEFAULT_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}
"""Added to a response that does not set the header itself (with the framing headers)."""

_FRAME_OPTIONS = {"'none'": "DENY", "'self'": "SAMEORIGIN"}
"""``X-Frame-Options`` for the frame-ancestors values it can express.

It cannot name other origins, so for any other value it is left out and browsers
use the CSP alone (they ignore ``X-Frame-Options`` when the CSP has frame-ancestors).
"""


def framing_headers(frame_ancestors: str = DEFAULT_FRAME_ANCESTORS) -> dict[str, str]:
    """The CSP and ``X-Frame-Options`` headers for a frame-ancestors source list.

    Args:
        frame_ancestors: the directive's sources, e.g. ``'none'`` or ``'self'``.

    Returns:
        ``Content-Security-Policy`` and, when it can express them, ``X-Frame-Options``.
    """
    headers = {"Content-Security-Policy": f"frame-ancestors {frame_ancestors}"}
    if frame_ancestors in _FRAME_OPTIONS:
        headers["X-Frame-Options"] = _FRAME_OPTIONS[frame_ancestors]
    return headers


class SecurityHeadersMiddleware:
    """Add the security headers to every HTTP response.

    Args:
        app: the app to wrap.
        frame_ancestors: who may frame the responses (CSP ``frame-ancestors`` sources).
    """

    def __init__(self, app: ASGIApp, frame_ancestors: str = DEFAULT_FRAME_ANCESTORS) -> None:
        self.app = app
        self.directive = f"frame-ancestors {frame_ancestors}"
        self.headers = {**framing_headers(frame_ancestors), **DEFAULT_HEADERS}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in self.headers.items():
                    if name not in headers:
                        headers[name] = value
                csp = headers["Content-Security-Policy"]
                if "frame-ancestors" not in csp.lower():
                    headers["Content-Security-Policy"] = f"{csp}; {self.directive}"
            await send(message)

        await self.app(scope, receive, send_with_headers)

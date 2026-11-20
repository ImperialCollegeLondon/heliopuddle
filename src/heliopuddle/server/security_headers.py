"""Security response headers for the API and the UI (AUTH-19).

A small ASGI middleware: it adds the headers below to every response and never
replaces one a route already set. A route's own ``Content-Security-Policy`` is
kept, with ``frame-ancestors 'none'`` appended when it has none. It imports
nothing from the database layer, so the UI may use it.
"""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

FRAME_ANCESTORS = "frame-ancestors 'none'"

DEFAULT_HEADERS = {
    "Content-Security-Policy": FRAME_ANCESTORS,
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}
"""Added to a response that does not set the header itself."""


class SecurityHeadersMiddleware:
    """Add :data:`DEFAULT_HEADERS` to every HTTP response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in DEFAULT_HEADERS.items():
                    if name not in headers:
                        headers[name] = value
                csp = headers["Content-Security-Policy"]
                if "frame-ancestors" not in csp.lower():
                    headers["Content-Security-Policy"] = f"{csp}; {FRAME_ANCESTORS}"
            await send(message)

        await self.app(scope, receive, send_with_headers)

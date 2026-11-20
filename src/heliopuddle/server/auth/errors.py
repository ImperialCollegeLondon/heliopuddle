"""Errors raised by the auth layer; the API turns them into HTTP responses."""

from __future__ import annotations


class AuthError(Exception):
    """Credentials are missing or invalid: HTTP 401 with ``WWW-Authenticate: Bearer``.

    Args:
        detail: a short message safe to show the caller (never a secret or token).
        status_code: 401 by default; 502 when the identity provider cannot be reached (AUTH-10).
    """

    def __init__(self, detail: str, status_code: int = 401) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code

"""UI login, logout and sessions (AUTH-16).

* :mod:`~heliopuddle.server.ui.auth.session`: the signed session cookie, the
  signed-in user and the guard that sends anonymous requests to ``/login``.
* :mod:`~heliopuddle.server.ui.auth.oidc`: PKCE, absolute URLs and the provider cache.
* :mod:`~heliopuddle.server.ui.auth.routes`: the ``/login``, ``/logout`` and
  ``/auth/callback`` pages (``login.html`` is beside it).

This package root does not import the routes, so that
:mod:`heliopuddle.server.ui.shared` can use the session without a cycle.
"""

from heliopuddle.server.ui.auth.oidc import OidcProviderCache
from heliopuddle.server.ui.auth.session import (
    SESSION_COOKIE,
    LoginRequired,
    SessionUser,
    current_target,
    install_session,
    login_redirect,
    make_guard,
    read_user,
    safe_next,
    session_key,
)

__all__ = [
    "SESSION_COOKIE",
    "LoginRequired",
    "OidcProviderCache",
    "SessionUser",
    "current_target",
    "install_session",
    "login_redirect",
    "make_guard",
    "read_user",
    "safe_next",
    "session_key",
]

"""Auth initialisation at server startup (called by ``server/start.py``)."""

from __future__ import annotations

from heliopuddle.server.context import AppContext


class AuthStartupError(Exception):
    """Auth cannot be initialised; startup stops with this message (exit code 2)."""


def init_auth(ctx: AppContext) -> None:
    """Prepare authentication for ``ctx.settings.auth_mode``.

    ``local``: load or create the signing key, then create the first admin.
    ``oidc``: discover the provider and store it on ``ctx.oidc``. ``header``: nothing.

    Raises:
        AuthStartupError: with a message naming the setting or the cause.
    """
    from heliopuddle.server.auth import bootstrap, oidc, secret

    mode = ctx.settings.auth_mode
    try:
        if mode == "local":
            secret.load_secret(ctx.settings)
            bootstrap.ensure_admin(ctx)
        elif mode == "oidc":
            ctx.oidc = oidc.OidcProvider.discover(ctx.settings)
    except (secret.SecretKeyError, oidc.OidcStartupError) as e:
        raise AuthStartupError(str(e)) from None

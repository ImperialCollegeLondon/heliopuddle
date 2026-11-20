"""OIDC resource server: validate provider tokens (AUTH-09, AUTH-10).

The API validates bearer JWTs locally against the provider's public keys, which
a ``jwt.PyJWKClient`` fetches from the JWKS endpoint (cached, and refetched on
an unknown ``kid`` at most once per cooldown), and only asks the provider about
a token when introspection applies. All I/O is synchronous: FastAPI runs sync
dependencies in a threadpool. See docs/design/auth.md.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import jwt
from jwt import PyJWKClient, PyJWKClientConnectionError

from heliopuddle.server.auth.errors import AuthError
from heliopuddle.server.auth.principal import Principal
from heliopuddle.server.auth.roles import valid_roles
from heliopuddle.settings import Settings

logger = logging.getLogger(__name__)

ALLOWED_ALGORITHMS = [
    "RS256",
    "RS384",
    "RS512",
    "ES256",
    "ES384",
    "ES512",
    "PS256",
    "PS384",
    "PS512",
]
"""Asymmetric algorithms only: ``HS*`` and ``none`` are never accepted."""

_ASYMMETRIC_KEY_TYPES = frozenset({"RSA", "EC"})
_DISCOVERY_TIMEOUT = 5.0
_JWKS_LIFESPAN = 3600.0
"""Seconds a fetched key set is trusted before it is fetched again."""
DEFAULT_JWKS_COOLDOWN = 10.0
"""Minimum seconds between key-set refetches caused by an unknown ``kid``."""
_UNAVAILABLE = "identity provider unavailable"


class OidcStartupError(Exception):
    """The OIDC provider cannot be used; startup stops with this message."""


class OidcProvider:
    """A discovered OIDC provider: validates bearer JWTs and introspects tokens.

    Build one with :meth:`discover`; it is thread-safe.

    Attributes:
        issuer: the issuer URL (no trailing slash).
        client_id: the OAuth client id (also the default audience).
        token_url: the provider's token endpoint.
        authorization_url: the provider's authorization endpoint, if it has one.
        end_session_url: the provider's ``end_session_endpoint`` (RP-initiated logout), if any.
        introspection_url: the introspection endpoint (None only when introspection is off).
    """

    def __init__(
        self,
        settings: Settings,
        config: dict[str, Any],
        http_client: httpx.Client,
        jwks_client: PyJWKClient,
    ) -> None:
        self._settings = settings
        self._http = http_client
        self.issuer = settings.resolved_oidc_issuer
        self.client_id = settings.oidc_client_id or ""
        self._audience = settings.resolved_oidc_audience
        self.token_url: str = config["token_endpoint"]
        self.authorization_url: str | None = config.get("authorization_endpoint")
        end_session = config.get("end_session_endpoint")
        self.end_session_url: str | None = end_session if isinstance(end_session, str) else None
        self.introspection_url: str | None = config.get("introspection_endpoint")
        self._jwks = jwks_client

    # ------------------------------------------------------------- discovery

    @classmethod
    def discover(
        cls,
        settings: Settings,
        http_client: httpx.Client | None = None,
        *,
        jwks_cooldown: float = DEFAULT_JWKS_COOLDOWN,
    ) -> OidcProvider:
        """Fetch the discovery document and the JWKS (fail fast).

        Args:
            settings: the validated settings (``oidc_*``).
            http_client: the client for the discovery, introspection and token
                requests; tests inject one with a mock transport. Default: a
                client with a short timeout. The JWKS is fetched by a
                ``jwt.PyJWKClient`` (urllib), not by this client.
            jwks_cooldown: minimum seconds between key-set refetches triggered by
                an unknown ``kid``, so a flood of bad tokens cannot hammer the
                provider (a key rotation is picked up after at most this long).

        Raises:
            OidcStartupError: the issuer is unreachable or inconsistent. The
                message names the setting at fault and never the client secret.
        """
        issuer = settings.resolved_oidc_issuer
        client = http_client or httpx.Client(timeout=_DISCOVERY_TIMEOUT)
        well_known = f"{issuer}/.well-known/openid-configuration"
        who = f"HELIOPUDDLE_OIDC_ISSUER ({issuer})"
        config = _get_json(client, well_known, f"{who}: cannot read the discovery document")
        if not isinstance(config, dict):
            raise OidcStartupError(f"{who}: the discovery document is not a JSON object")
        if str(config.get("issuer", "")).rstrip("/") != issuer:
            raise OidcStartupError(
                f"{who}: the discovery document names issuer {config.get('issuer')!r}, "
                "which differs from the setting"
            )
        if not isinstance(config.get("jwks_uri"), str):
            raise OidcStartupError(f"{who}: the discovery document has no jwks_uri")
        if not isinstance(config.get("token_endpoint"), str):
            raise OidcStartupError(f"{who}: the discovery document has no token_endpoint")
        if settings.oidc_introspect != "off" and not isinstance(
            config.get("introspection_endpoint"), str
        ):
            raise OidcStartupError(
                f"HELIOPUDDLE_OIDC_INTROSPECT is {settings.oidc_introspect}, but the "
                f"discovery document of {issuer} has no introspection_endpoint; set it to off "
                "or use a provider that supports introspection"
            )
        jwks = PyJWKClient(
            config["jwks_uri"],
            lifespan=_JWKS_LIFESPAN,
            timeout=_DISCOVERY_TIMEOUT,
            cooldown_duration=jwks_cooldown,
        )
        try:
            keys = jwks.get_signing_keys()
        except jwt.PyJWTError as exc:
            raise OidcStartupError(
                f"{who}: cannot fetch the JWKS ({config['jwks_uri']}): {exc}"
            ) from None
        if not any(k.key_type in _ASYMMETRIC_KEY_TYPES for k in keys):
            raise OidcStartupError(f"{who}: the JWKS has no usable asymmetric signing key")
        return cls(settings, config, client, jwks)

    def close(self) -> None:
        """Close the HTTP client."""
        self._http.close()

    @property
    def token_endpoint(self) -> str:
        """The token endpoint (an alias of :attr:`token_url`), for exchanging codes."""
        return self.token_url

    # ---------------------------------------------------------- authenticate

    def authenticate(self, token: str) -> Principal:
        """Validate a bearer JWT locally and return its principal.

        The signature is checked with the provider's key for the token's
        ``kid``; ``iss``, ``aud`` and ``exp`` must be present and valid.

        Raises:
            AuthError: 401 when the token is invalid; 502 when an unknown key
                needs a JWKS refetch and the provider cannot be reached.
        """
        try:
            kid = jwt.get_unverified_header(token).get("kid")
            if not isinstance(kid, str):
                raise jwt.InvalidTokenError("no kid")
            signing_key = self._jwks.get_signing_key(kid)
            if signing_key.key_type not in _ASYMMETRIC_KEY_TYPES:
                raise jwt.InvalidTokenError("not an asymmetric key")
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=ALLOWED_ALGORITHMS,
                audience=self._audience,
                issuer=self.issuer,
                options={"require": ["exp", "iss", "aud"]},
            )
        except PyJWKClientConnectionError:
            logger.warning("could not refetch the JWKS from %s", self._jwks.uri)
            raise AuthError(_UNAVAILABLE, 502) from None
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            raise AuthError("invalid token") from None
        username = claims.get(self._settings.oidc_username_claim)
        if not isinstance(username, str) or not username:
            username = claims.get("sub")
        if not isinstance(username, str) or not username:
            raise AuthError("invalid token")
        roles = _claim_by_path(claims, self._settings.oidc_roles_claim)
        names = [r for r in roles if isinstance(r, str)] if isinstance(roles, list) else []
        return Principal(username, tuple(valid_roles(names)), "oidc", token=token)

    # --------------------------------------------------------- introspection

    def introspect(self, token: str) -> None:
        """Ask the provider whether ``token`` is still active (never cached).

        Raises:
            AuthError: 401 when inactive, 502 when the provider cannot be reached,
                answers with an error status or sends an unreadable reply.
        """
        if self.introspection_url is None:
            raise AuthError("token introspection is not available", 502)
        secret = self._settings.oidc_client_secret
        auth = httpx.BasicAuth(self.client_id, secret.get_secret_value() if secret else "")
        try:
            response = self._http.post(
                self.introspection_url,
                data={"token": token, "token_type_hint": "access_token"},
                auth=auth,
            )
        except httpx.HTTPError:
            logger.warning("token introspection failed: provider unreachable")
            raise AuthError(_UNAVAILABLE, 502) from None
        if response.status_code != 200:
            logger.warning("token introspection failed: HTTP %s", response.status_code)
            raise AuthError(_UNAVAILABLE, 502)
        try:
            body = response.json()
        except ValueError:
            logger.warning("token introspection failed: unreadable reply")
            raise AuthError(_UNAVAILABLE, 502) from None
        if not isinstance(body, dict) or body.get("active") is not True:
            raise AuthError("token is not active")

    # ----------------------------------------------------------- token endpoint

    def token_request(self, params: dict[str, str]) -> dict[str, Any]:
        """POST a form to the token endpoint with the client credentials.

        Used by the UI to exchange an authorization code for tokens.

        Args:
            params: the form fields (``grant_type``, ``code``, ``redirect_uri``, ...);
                ``client_id`` and ``client_secret`` are added.

        Returns:
            The provider's JSON reply.

        Raises:
            AuthError: 401 when the provider refuses the request, 502 when it
                cannot be reached or replies unreadably.
        """
        data = {**params, "client_id": self.client_id}
        if self._settings.oidc_client_secret is not None:
            data["client_secret"] = self._settings.oidc_client_secret.get_secret_value()
        try:
            response = self._http.post(self.token_url, data=data)
        except httpx.HTTPError:
            logger.warning("token request failed: provider unreachable")
            raise AuthError(_UNAVAILABLE, 502) from None
        if 400 <= response.status_code < 500:
            raise AuthError("the identity provider refused the request")
        try:
            body = response.json() if response.status_code == 200 else None
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise AuthError(_UNAVAILABLE, 502)
        return body


# ------------------------------------------------------------------- helpers


def _get_json(client: httpx.Client, url: str, what: str) -> Any:
    """GET ``url`` and parse JSON; any failure is an :class:`OidcStartupError`."""
    try:
        response = client.get(url)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        raise OidcStartupError(f"{what} ({url}): HTTP {exc.response.status_code}") from None
    except httpx.HTTPError as exc:
        raise OidcStartupError(f"{what} ({url}): {exc}") from None
    except ValueError:
        raise OidcStartupError(f"{what} ({url}): the reply is not valid JSON") from None


def _claim_by_path(claims: dict[str, Any], path: str) -> Any:
    """Read ``claims`` by a dotted path (a flat key containing dots wins)."""
    if path in claims:
        return claims[path]
    value: Any = claims
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value

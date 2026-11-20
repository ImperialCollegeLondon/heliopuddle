"""A fake OIDC issuer for tests: discovery, JWKS, token and introspection endpoints.

No sockets or ports are used. Discovery, token and introspection requests go
through :class:`httpx.MockTransport`: hand the issuer's :meth:`FakeIssuer.client`
to ``OidcProvider.discover`` as ``http_client``. The JWKS is fetched by the real
``jwt.PyJWKClient`` over ``urllib``; the autouse fixture ``_fake_jwks_network``
in ``tests/conftest.py`` calls :func:`install_jwks_network`, which makes
``urllib`` answer for every issuer created in the test (so PyJWT's caching,
cooldown and error handling all run for real). Tokens are signed with a
generated RSA key; the issuer can rotate it, go down, and answer introspection
as active or inactive.

Example:
    >>> issuer = FakeIssuer()
    >>> token = issuer.token(username="ann", roles=["hp-reader"])
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

DEFAULT_ISSUER = "https://idp.test/realms/hp"
DEFAULT_CLIENT_ID = "hp"
DEFAULT_CLIENT_SECRET = "s3cret-value"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@dataclass
class RecordedRequest:
    """One request the fake issuer received."""

    method: str
    path: str
    headers: dict[str, str]
    form: dict[str, str]


@dataclass
class FakeIssuer:
    """An in-memory identity provider.

    Attributes:
        issuer: the issuer URL (no trailing slash).
        client_id: the client id the issuer accepts for introspection.
        client_secret: the client secret the issuer accepts for introspection.
        discovery: the discovery document; edit it to simulate a bad one.
        jwks_calls: how many times the JWKS endpoint was fetched.
        requests: every request received, in order.
        introspect_status: HTTP status of the introspection endpoint.
        token_response: the JSON body the token endpoint returns (``None``: a
            fresh access token for ``token_user``).
    """

    issuer: str = DEFAULT_ISSUER
    client_id: str = DEFAULT_CLIENT_ID
    client_secret: str = DEFAULT_CLIENT_SECRET
    discovery: dict[str, Any] = field(default_factory=dict)
    jwks_calls: int = 0
    requests: list[RecordedRequest] = field(default_factory=list)
    introspect_status: int = 200
    token_response: dict[str, Any] | None = None
    jwks_extra_keys: list[dict[str, Any]] = field(default_factory=list)
    _active: bool = True
    _down: bool = False
    _jwks_down: bool = False
    _generation: int = 0
    _keys: dict[str, rsa.RSAPrivateKey] = field(default_factory=dict)
    _current_kid: str = ""

    def __post_init__(self) -> None:
        self.issuer = self.issuer.rstrip("/")
        self.discovery = {
            "issuer": self.issuer,
            "jwks_uri": f"{self.issuer}/protocol/openid-connect/certs",
            "token_endpoint": f"{self.issuer}/protocol/openid-connect/token",
            "authorization_endpoint": f"{self.issuer}/protocol/openid-connect/auth",
            "end_session_endpoint": f"{self.issuer}/protocol/openid-connect/logout",
            "introspection_endpoint": f"{self.issuer}/protocol/openid-connect/token/introspect",
            **self.discovery,
        }
        self.rotate_key()
        _REGISTRY[self.discovery["jwks_uri"]] = self

    # ------------------------------------------------------------------ keys

    @property
    def kid(self) -> str:
        """The key id tokens are currently signed with."""
        return self._current_kid

    @property
    def private_key(self) -> rsa.RSAPrivateKey:
        """The current signing key."""
        return self._keys[self._current_kid]

    def rotate_key(self, keep_old: bool = False) -> str:
        """Generate a new signing key and publish it in the JWKS.

        Args:
            keep_old: also keep publishing the previous key.

        Returns:
            The new key id.
        """
        self._generation += 1
        kid = f"key-{self._generation}"
        if not keep_old:
            self._keys.clear()
        self._keys[kid] = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self._current_kid = kid
        return kid

    def public_pem(self) -> bytes:
        """The current public key in PEM form (the secret in an algorithm-confusion attack)."""
        return self.private_key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )

    def jwks(self) -> dict[str, Any]:
        """The JWKS document currently served."""
        keys: list[dict[str, Any]] = []
        for kid, key in self._keys.items():
            jwk = RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
            jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
            keys.append(jwk)
        return {"keys": [*keys, *self.jwks_extra_keys]}

    # ---------------------------------------------------------------- tokens

    def token(
        self,
        *,
        sub: str = "user-1",
        username: str | None = "ann",
        roles: list[str] | None = None,
        aud: str | list[str] | None = DEFAULT_CLIENT_ID,
        iss: str | None = None,
        exp_delta: float | None = 300,
        extra_claims: dict[str, Any] | None = None,
        alg: str = "RS256",
        kid: str | None = None,
    ) -> str:
        """Mint an access token.

        Args:
            sub: the ``sub`` claim.
            username: the ``preferred_username`` claim (omitted when None).
            roles: the realm roles, under ``realm_access.roles`` (claim omitted when None).
            aud: the ``aud`` claim (omitted when None).
            iss: the ``iss`` claim (default: this issuer).
            exp_delta: seconds from now until expiry (negative: expired; None: no ``exp``).
            extra_claims: merged over the claims (a value of None removes the claim).
            alg: ``RS256`` (and other RS/PS), ``HS256`` (the public key PEM used as the
                HMAC secret: algorithm confusion) or ``none`` (unsigned).
            kid: the header ``kid`` (default: the current key).
        """
        now = int(time.time())
        claims: dict[str, Any] = {"sub": sub, "iat": now, "iss": iss or self.issuer}
        if username is not None:
            claims["preferred_username"] = username
        if roles is not None:
            claims["realm_access"] = {"roles": roles}
        if aud is not None:
            claims["aud"] = aud
        if exp_delta is not None:
            claims["exp"] = now + exp_delta
        for key, value in (extra_claims or {}).items():
            if value is None:
                claims.pop(key, None)
            else:
                claims[key] = value
        header: dict[str, Any] = {"alg": alg, "typ": "JWT", "kid": kid or self._current_kid}
        if alg == "none":
            return f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}."
        if alg.startswith("HS"):
            signing_input = (
                f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}"
            )
            digest = getattr(hashlib, "sha" + alg[2:])
            mac = hmac.new(self.public_pem(), signing_input.encode(), digest).digest()
            return f"{signing_input}.{_b64(mac)}"
        return jwt.encode(claims, self.private_key, algorithm=alg, headers={"kid": header["kid"]})

    # ------------------------------------------------------------ behaviours

    def set_active(self, active: bool) -> None:
        """Make introspection answer ``active`` true or false."""
        self._active = active

    def down(self, jwks_only: bool = False) -> None:
        """Simulate an outage: connections fail (``jwks_only``: just the JWKS endpoint)."""
        if jwks_only:
            self._jwks_down = True
        else:
            self._down = True

    def up(self) -> None:
        """End the outage."""
        self._down = False
        self._jwks_down = False

    def requests_to(self, suffix: str) -> list[RecordedRequest]:
        """The requests whose path ends with ``suffix``."""
        return [r for r in self.requests if r.path.endswith(suffix)]

    # ------------------------------------------------------------------ http

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.requests.append(RecordedRequest(request.method, path, dict(request.headers), form))
        if self._down:
            raise httpx.ConnectError("connection refused", request=request)
        if path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json=self.discovery)
        if path.endswith("/token/introspect"):
            if self.introspect_status != 200:
                return httpx.Response(self.introspect_status, text="error")
            expected = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
            if request.headers.get("authorization") != f"Basic {expected}":
                return httpx.Response(401, json={"error": "invalid_client"})
            return httpx.Response(200, json={"active": self._active})
        if path.endswith("/token"):
            body = self.token_response or {
                "access_token": self.token(roles=["hp-reader"]),
                "token_type": "Bearer",
                "expires_in": 300,
            }
            return httpx.Response(200, json=body)
        return httpx.Response(404)

    def client(self) -> httpx.Client:
        """An :class:`httpx.Client` wired to this issuer (no network)."""
        return httpx.Client(transport=httpx.MockTransport(self._handle))

    def _serve_jwks(self, url: str) -> io.BytesIO:
        """Answer a ``urllib`` request for the JWKS (what ``PyJWKClient`` sends)."""
        self.requests.append(RecordedRequest("GET", urlparse(url).path, {}, {}))
        if self._down or self._jwks_down:
            raise urllib.error.URLError("connection refused")
        self.jwks_calls += 1
        return io.BytesIO(json.dumps(self.jwks()).encode())


# ------------------------------------------------------------- urllib network

_REGISTRY: dict[str, FakeIssuer] = {}
"""Issuers by JWKS URL, for the ``urllib`` stand-in below."""


class _RoutingOpener:
    """Answers JWKS requests for registered issuers, delegates everything else."""

    def __init__(self, real: urllib.request.OpenerDirector) -> None:
        self._real = real

    def open(self, fullurl: Any, *args: Any, **kwargs: Any) -> Any:
        url = fullurl.full_url if isinstance(fullurl, urllib.request.Request) else str(fullurl)
        issuer = _REGISTRY.get(url)
        if issuer is None:
            return self._real.open(fullurl, *args, **kwargs)
        return issuer._serve_jwks(url)


@contextmanager
def install_jwks_network() -> Iterator[None]:
    """Make ``urllib`` serve the JWKS of every :class:`FakeIssuer` created meanwhile.

    ``PyJWKClient`` builds an opener per fetch with ``urllib.request.build_opener``;
    this wraps it so JWKS URLs of registered issuers never touch the network.
    """
    real_build_opener = urllib.request.build_opener

    def build_opener(*handlers: Any) -> Any:
        return _RoutingOpener(real_build_opener(*handlers))

    urllib.request.build_opener = build_opener  # type: ignore[assignment]
    _REGISTRY.clear()
    try:
        yield
    finally:
        urllib.request.build_opener = real_build_opener
        _REGISTRY.clear()

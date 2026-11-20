"""UI login through an OIDC provider (AUTH-16, AUTH-09)."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import pytest
from fastapi.testclient import TestClient

from heliopuddle.server.auth.oidc import OidcProvider
from heliopuddle.server.ui import create_ui
from heliopuddle.settings import Settings
from tests.fake_oidc import DEFAULT_CLIENT_ID, DEFAULT_CLIENT_SECRET, FakeIssuer
from tests.unit._ui_auth_helpers import PASSWORD, SECRET, AuthFactory, location, ui, with_roles
from tests.unit._ui_helpers import (
    make_file,
)

pytestmark = pytest.mark.req("AUTH-16")


@dataclass
class Oidc:
    """An OIDC UI wired to a fake issuer."""

    issuer: FakeIssuer
    factory: AuthFactory
    browser: TestClient
    settings: Settings
    discovered: list[OidcProvider] = field(default_factory=list)
    prefix: str = ""
    """The path prefix browsers use (the UI root path)."""

    def start(self, **params: str) -> Any:
        return self.browser.get(f"{self.prefix}/login", params=params, follow_redirects=False)

    def authorize(self, **params: str) -> dict[str, str]:
        response = self.start(**params)
        assert response.status_code == 303, response.text
        parts = urlsplit(response.headers["location"])
        assert (
            f"{parts.scheme}://{parts.netloc}{parts.path}"
            == (self.issuer.discovery["authorization_endpoint"])
        )
        return {k: v[0] for k, v in parse_qs(parts.query).items()}

    def callback(self, **params: str) -> Any:
        return self.browser.get(
            f"{self.prefix}/auth/callback", params=params, follow_redirects=False
        )

    def token_requests(self) -> list[dict[str, str]]:
        return [r.form for r in self.issuer.requests_to("/protocol/openid-connect/token")]


def oidc_ui(
    issuer: FakeIssuer | None = None, roles: list[str] | None = None, **settings: Any
) -> Oidc:
    issuer = issuer or FakeIssuer()
    values: dict[str, Any] = {
        "auth_mode": "oidc",
        "auth_secret_key": SECRET,
        "oidc_issuer": issuer.issuer,
        "oidc_client_id": DEFAULT_CLIENT_ID,
        "oidc_client_secret": DEFAULT_CLIENT_SECRET,
        **settings,
    }
    s = Settings(**values)
    factory = AuthFactory(files=[make_file(1)])
    issuer.token_response = {"access_token": "idp-token", "token_type": "Bearer", "expires_in": 300}
    factory.identities["idp-token"] = ("ann", roles if roles is not None else ["hp-reader"])
    discovered: list[OidcProvider] = []

    def make() -> OidcProvider:
        provider = OidcProvider.discover(s, http_client=issuer.client())
        discovered.append(provider)
        return provider

    app = create_ui(s, client_factory=factory, oidc_provider=make)
    prefix = s.ui_root_path.rstrip("/")
    browser = TestClient(app, raise_server_exceptions=False)
    return Oidc(issuer, factory, browser, s, discovered, prefix)


def b64sha256(value: str) -> str:
    digest = hashlib.sha256(value.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def logged_in_oidc(**kw: Any) -> Oidc:
    issuer = kw.pop("issuer", None)
    o = oidc_ui(issuer, **kw)
    q = o.authorize()
    assert o.callback(code="c", state=q["state"]).status_code == 303
    return o


def test_oidc_login_redirects_to_the_provider_with_state_and_pkce() -> None:
    o = oidc_ui()
    q = o.authorize(next="/projects/demo")
    assert q["response_type"] == "code"
    assert q["client_id"] == DEFAULT_CLIENT_ID
    assert q["redirect_uri"] == "http://testserver/auth/callback"
    assert q["scope"] == "openid"
    assert q["code_challenge_method"] == "S256"
    assert len(q["state"]) >= 32
    assert len(q["code_challenge"]) == 43  # base64url of a sha256 digest
    assert "client_secret" not in q
    assert o.discovered and len(o.discovered) == 1
    # every login uses a new state and challenge
    again = o.authorize()
    assert again["state"] != q["state"]
    assert again["code_challenge"] != q["code_challenge"]
    assert len(o.discovered) == 1  # discovery is cached


def test_oidc_code_exchange_and_session() -> None:
    o = oidc_ui(roles=["hp-writer"])
    q = o.authorize(next="/projects/demo?limit=5")
    response = o.callback(code="the-code", state=q["state"])
    assert location(response) == "/projects/demo?limit=5"
    (form,) = o.token_requests()
    assert form["grant_type"] == "authorization_code"
    assert form["code"] == "the-code"
    assert form["redirect_uri"] == q["redirect_uri"]
    assert form["client_id"] == DEFAULT_CLIENT_ID
    assert form["client_secret"] == DEFAULT_CLIENT_SECRET
    assert b64sha256(form["code_verifier"]) == q["code_challenge"]  # PKCE S256
    assert 43 <= len(form["code_verifier"]) <= 128
    page = o.browser.get("/projects/demo")
    assert page.status_code == 200
    assert 'data-user="ann"' in page.text
    assert "hp-writer" in page.text
    assert "idp-token" not in page.text
    assert o.factory.made[-1].token == "idp-token"
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie


@pytest.mark.parametrize("bad_next", ["//evil.example", "https://evil.example", "/\\evil"])
def test_oidc_next_is_validated(bad_next: str) -> None:
    o = oidc_ui()
    q = o.authorize(next=bad_next)
    assert location(o.callback(code="c", state=q["state"])) == "/"


def test_oidc_state_mismatch_is_rejected_without_a_token_request() -> None:
    o = oidc_ui()
    q = o.authorize()
    wrong = o.callback(code="c", state=q["state"][:-1] + ("A" if q["state"][-1] != "A" else "B"))
    assert wrong.status_code == 400
    assert not o.token_requests()
    # the flow is single use: even the right state no longer works
    assert o.callback(code="c", state=q["state"]).status_code == 400
    assert not o.token_requests()
    assert location(o.browser.get("/projects/demo", follow_redirects=False)).startswith("/login")


@pytest.mark.parametrize("params", [{}, {"code": "c"}, {"state": "s"}, {"code": "c", "state": ""}])
def test_oidc_callback_without_a_started_flow_is_rejected(params: dict[str, str]) -> None:
    o = oidc_ui()
    assert o.callback(**params).status_code == 400
    assert not o.token_requests()


def test_oidc_callback_replay_is_rejected() -> None:
    o = oidc_ui()
    q = o.authorize()
    assert o.callback(code="c", state=q["state"]).status_code == 303
    assert o.callback(code="c", state=q["state"]).status_code == 400
    assert len(o.token_requests()) == 1


def test_oidc_provider_error_on_callback() -> None:
    o = oidc_ui()
    q = o.authorize()
    response = o.callback(error="access_denied", state=q["state"])
    assert response.status_code == 401
    assert "Login failed" in response.text
    assert not o.token_requests()


def test_oidc_token_response_without_an_access_token() -> None:
    o = oidc_ui()
    o.issuer.token_response = {"error": "weird"}
    q = o.authorize()
    assert o.callback(code="c", state=q["state"]).status_code == 401


def test_oidc_api_refusing_the_new_token_is_a_login_failure() -> None:
    o = oidc_ui()
    o.factory.revoked.add("idp-token")
    q = o.authorize()
    response = o.callback(code="c", state=q["state"])
    assert response.status_code == 401
    assert "set-cookie" not in response.headers or "idp-token" not in response.text
    assert location(o.browser.get("/", follow_redirects=False)).startswith("/login")


def test_oidc_provider_down_is_an_error_page_and_is_retried() -> None:
    issuer = FakeIssuer()
    o = oidc_ui(issuer)
    issuer.down()
    response = o.start()
    assert response.status_code == 503
    assert "identity provider is unavailable" in response.text
    assert issuer.discovery["token_endpoint"] not in response.text
    issuer.up()
    assert o.start().status_code == 303  # nothing was cached from the failure


def test_oidc_provider_down_during_the_callback() -> None:
    issuer = FakeIssuer()
    o = oidc_ui(issuer)
    q = o.authorize()
    issuer.down()
    assert o.callback(code="c", state=q["state"]).status_code == 503


def test_oidc_provider_is_discovered_lazily() -> None:
    o = oidc_ui()
    assert not o.discovered
    assert o.browser.get("/static/hp.css").status_code == 200
    assert location(o.browser.get("/projects/demo", follow_redirects=False)).startswith("/login")
    assert not o.discovered  # the guard alone never calls the provider
    o.start()
    assert len(o.discovered) == 1


def test_oidc_expiry_logs_in_again_through_the_provider() -> None:
    o = oidc_ui()
    o.issuer.token_response = {"access_token": "idp-token", "expires_in": -10}
    q = o.authorize(next="/projects/demo")
    assert location(o.callback(code="c", state=q["state"])) == "/projects/demo"
    response = o.browser.get("/projects/demo", follow_redirects=False)
    assert location(response) == "/login?next=%2Fprojects%2Fdemo"
    again = o.browser.get(location(response), follow_redirects=False)
    assert again.headers["location"].startswith(o.issuer.discovery["authorization_endpoint"])


def test_oidc_logout_ends_the_provider_session() -> None:
    o = logged_in_oidc()
    response = o.browser.post("/logout", follow_redirects=False)
    target = urlsplit(location(response))
    assert (
        f"{target.scheme}://{target.netloc}{target.path}"
        == (o.issuer.discovery["end_session_endpoint"])
    )
    query = {k: v[0] for k, v in parse_qs(target.query).items()}
    assert query == {
        "client_id": DEFAULT_CLIENT_ID,
        "post_logout_redirect_uri": "http://testserver/login?logged_out=1",
    }
    assert "id_token_hint" not in query
    assert "1970" in response.headers["set-cookie"]  # hp's own session is gone too
    assert location(o.browser.get("/", follow_redirects=False)) == "/login"


def test_oidc_logout_without_an_end_session_endpoint_stays_local() -> None:
    issuer = FakeIssuer()
    del issuer.discovery["end_session_endpoint"]
    o = logged_in_oidc(issuer=issuer)
    response = o.browser.post("/logout", follow_redirects=False)
    assert location(response) == "/login?logged_out=1"
    assert "1970" in response.headers["set-cookie"]
    page = o.browser.get("/login?logged_out=1")
    assert "You have been logged out." in page.text
    assert "single sign-on" in page.text


def test_oidc_logout_when_the_provider_is_unreachable_still_logs_out_of_hp() -> None:
    issuer = FakeIssuer()
    o = oidc_ui(issuer)  # nothing discovered yet
    issuer.down()
    response = o.browser.post("/logout", follow_redirects=False)
    assert location(response) == "/login?logged_out=1"


def test_oidc_post_logout_url_honours_root_path_and_proxy_headers() -> None:
    o = logged_in_oidc(ui_root_path="/hp")
    response = o.browser.post(
        "/hp/logout",
        headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "hp.example.org"},
        follow_redirects=False,
    )
    query = parse_qs(urlsplit(location(response)).query)
    assert query["post_logout_redirect_uri"] == ["https://hp.example.org/hp/login?logged_out=1"]
    plain = logged_in_oidc(ui_root_path="/hp")
    redirect = location(plain.browser.post("/hp/logout", follow_redirects=False))
    assert parse_qs(urlsplit(redirect).query)["post_logout_redirect_uri"] == [
        "http://testserver/hp/login?logged_out=1"
    ]


def test_local_logout_never_goes_to_a_provider() -> None:
    _, browser = with_roles("hp-reader")
    assert location(browser.post("/logout", follow_redirects=False)) == "/login?logged_out=1"


def test_oidc_redirect_uri_includes_the_root_path() -> None:
    o = oidc_ui(ui_root_path="/hp")
    q = o.authorize(next="/hp/projects/demo")
    assert q["redirect_uri"] == "http://testserver/hp/auth/callback"
    assert location(o.callback(code="c", state=q["state"])) == "/hp/projects/demo"
    assert o.token_requests()[-1]["redirect_uri"] == q["redirect_uri"]


def test_oidc_redirect_uri_follows_the_proxy_headers() -> None:
    o = oidc_ui(ui_root_path="/hp")
    response = o.browser.get(
        "/hp/login",
        headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "hp.example.org"},
        follow_redirects=False,
    )
    query = {k: v[0] for k, v in parse_qs(urlsplit(response.headers["location"]).query).items()}
    assert query["redirect_uri"] == "https://hp.example.org/hp/auth/callback"
    assert "secure" in response.headers["set-cookie"].lower()


def test_oidc_login_form_posts_are_not_accepted() -> None:
    o = oidc_ui()
    response = o.browser.post(
        "/login", data={"username": "ann", "password": PASSWORD}, follow_redirects=False
    )
    assert response.status_code == 303
    assert "set-cookie" not in response.headers
    assert not o.factory.made


def test_next_is_encoded_once() -> None:
    # next values are encoded once: a literal '%' in a path survives the round trip
    browser = ui(AuthFactory())
    response = browser.get("/projects/demo?path=a%25b", follow_redirects=False)
    assert location(response) == f"/login?next={quote('/projects/demo?path=a%25b', safe='')}"

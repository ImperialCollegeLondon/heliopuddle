"""Token introspection and sensitive routes under OIDC (AUTH-07, AUTH-10)."""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from heliopuddle.server.api import create_api
from heliopuddle.server.api.deps import get_principal
from heliopuddle.server.auth.errors import AuthError
from heliopuddle.server.auth.principal import Principal
from heliopuddle.server.auth.roles import (
    ROLE_ADMIN,
    ROLE_DELETER,
    ROLE_OPERATOR,
    ROLE_READER,
    ROLE_WRITER,
)
from heliopuddle.server.context import AppContext
from tests.unit import _auth_enforcement_helpers as _fixtures
from tests.unit._auth_enforcement_helpers import STORAGE_ROLE, Act, World

act_as = _fixtures.act_as
app = _fixtures.app
client = _fixtures.client
config_dir = _fixtures.config_dir
world = _fixtures.world


class FakeOidc:
    """Stands in for ``OidcProvider``: records introspected tokens, can fail."""

    def __init__(self, error: AuthError | None = None) -> None:
        self.error = error
        self.tokens: list[str] = []

    def introspect(self, token: str) -> None:
        self.tokens.append(token)
        if self.error is not None:
            raise self.error


@pytest.fixture
def oidc_app(ctx: AppContext) -> Iterator[Callable[..., tuple[TestClient, FakeOidc]]]:
    clients: list[TestClient] = []

    def build(
        mode: str = "sensitive",
        error: AuthError | None = None,
        role: str = ROLE_ADMIN,
    ) -> tuple[TestClient, FakeOidc]:
        ctx.settings = ctx.settings.model_copy(
            update={
                "auth_mode": "oidc",
                "oidc_issuer": "https://kc/realms/r",
                "oidc_client_id": "hp",
                "oidc_client_secret": SecretStr("s"),
                "oidc_introspect": mode,
            }
        )
        fake = FakeOidc(error)
        ctx.oidc = fake  # type: ignore[assignment]
        application = create_api(ctx)
        application.dependency_overrides[get_principal] = lambda: Principal(
            "kc-user", (role,), "oidc", token="the-token"
        )
        c = TestClient(application)
        clients.append(c)
        return c, fake

    yield build
    for c in clients:
        c.close()


@pytest.mark.req("AUTH-10")
def test_sensitive_introspects_deleter_admin_and_credential_routes(
    oidc_app: Callable[..., tuple[TestClient, FakeOidc]], world: World
) -> None:
    client, fake = oidc_app("sensitive")
    assert client.get("/projects/demo/files").status_code == 200
    assert client.patch(f"/files/{world.demo_file}/metadata", json={"a": 1}).status_code == 200
    assert client.post("/projects/demo/runs", json={}).status_code == 422
    assert fake.tokens == []
    assert client.delete(f"/files/{world.demo_file}").status_code == 200
    assert fake.tokens == ["the-token"]
    assert client.get("/projects/demo/storage-locations/primary").status_code == 200
    assert fake.tokens == ["the-token", "the-token"]


@pytest.mark.req("AUTH-07", "AUTH-08", "AUTH-10", "API-03")
@pytest.mark.parametrize("role", [ROLE_ADMIN, STORAGE_ROLE, f"{STORAGE_ROLE}:demo"])
@pytest.mark.parametrize(
    ("error", "status"),
    [(None, 200), (AuthError("token is not active"), 401), (AuthError("unreachable", 502), 502)],
)
def test_storage_credentials_are_sensitive_for_global_and_scoped_roles(
    oidc_app: Callable[..., tuple[TestClient, FakeOidc]],
    role: str,
    error: AuthError | None,
    status: int,
) -> None:
    client, fake = oidc_app("sensitive", error, role)
    listed = client.get("/projects/demo/storage-locations")
    assert listed.status_code == status
    assert fake.tokens == ["the-token"]
    if status == 200:
        assert listed.json()[0]["url"]
    else:
        assert "url" not in listed.json()
    response = client.get("/projects/demo/storage-locations/primary")
    assert response.status_code == status
    assert fake.tokens == ["the-token", "the-token"]
    if status != 200:
        assert "storage_options" not in response.json()


@pytest.mark.req("AUTH-07", "AUTH-08", "AUTH-10", "API-03")
@pytest.mark.parametrize("role", [ROLE_READER, ROLE_WRITER, ROLE_DELETER, ROLE_OPERATOR])
@pytest.mark.parametrize("scoped", [False, True])
def test_redacted_storage_lists_do_not_need_sensitive_introspection(
    oidc_app: Callable[..., tuple[TestClient, FakeOidc]], role: str, scoped: bool
) -> None:
    client, fake = oidc_app(
        "sensitive", AuthError("provider unavailable", 502), f"{role}:demo" if scoped else role
    )
    listed = client.get("/projects/demo/storage-locations")
    assert listed.status_code == 200
    assert all(row["url"] == "" for row in listed.json())
    assert fake.tokens == []


@pytest.mark.req("AUTH-08", "AUTH-10", "API-03")
def test_other_project_credential_role_does_not_introspect_redacted_list(
    oidc_app: Callable[..., tuple[TestClient, FakeOidc]],
) -> None:
    client, fake = oidc_app("sensitive", AuthError("provider unavailable", 502))
    client.app.dependency_overrides[get_principal] = lambda: Principal(  # type: ignore[attr-defined]
        "kc-user", (f"{STORAGE_ROLE}:demo", "hp-reader:other"), "oidc", token="the-token"
    )
    listed = client.get("/projects/other/storage-locations")
    assert listed.status_code == 200
    assert all(row["url"] == "" for row in listed.json())
    assert fake.tokens == []


@pytest.mark.req("AUTH-10", "API-03")
@pytest.mark.parametrize("mode", ["always", "sensitive"])
def test_storage_list_introspects_only_once_per_request(
    oidc_app: Callable[..., tuple[TestClient, FakeOidc]], mode: str
) -> None:
    client, fake = oidc_app(mode)
    assert client.get("/projects/demo/storage-locations").status_code == 200
    assert fake.tokens == ["the-token"]


@pytest.mark.req("AUTH-10")
def test_always_introspects_every_route_once_per_request(
    oidc_app: Callable[..., tuple[TestClient, FakeOidc]], world: World
) -> None:
    client, fake = oidc_app("always")
    assert client.get("/projects/demo/files").status_code == 200
    assert client.get("/projects").status_code == 200
    assert fake.tokens == ["the-token", "the-token"]


@pytest.mark.req("AUTH-10")
def test_off_never_introspects(
    oidc_app: Callable[..., tuple[TestClient, FakeOidc]], world: World
) -> None:
    client, fake = oidc_app("off")
    assert client.delete(f"/files/{world.demo_file}").status_code == 200
    assert fake.tokens == []


@pytest.mark.req("AUTH-10")
def test_introspection_does_not_apply_outside_oidc_mode(
    client: TestClient, act_as: Act, world: World, ctx: AppContext
) -> None:
    fake = FakeOidc()
    ctx.oidc = fake  # type: ignore[assignment]
    act_as(ROLE_ADMIN)
    assert client.delete(f"/files/{world.demo_file}").status_code == 200
    assert fake.tokens == []


@pytest.mark.req("AUTH-10")
def test_inactive_token_is_401_before_anything_runs(
    oidc_app: Callable[..., tuple[TestClient, FakeOidc]], world: World
) -> None:
    client, _ = oidc_app("sensitive", AuthError("token is not active"))
    r = client.delete(f"/files/{world.demo_file}")
    assert r.status_code == 401
    assert r.headers["WWW-Authenticate"] == "Bearer"
    assert client.get(f"/files/{world.demo_file}").json()["deleted_at"] is None


@pytest.mark.req("AUTH-10")
def test_unreachable_issuer_is_502(
    oidc_app: Callable[..., tuple[TestClient, FakeOidc]], world: World
) -> None:
    client, _ = oidc_app("always", AuthError("identity provider unreachable", 502))
    r = client.get("/projects/demo")
    assert r.status_code == 502
    assert "WWW-Authenticate" not in r.headers


@pytest.mark.req("AUTH-10")
def test_introspection_runs_before_authorization(
    oidc_app: Callable[..., tuple[TestClient, FakeOidc]], world: World
) -> None:
    """A revoked token is a 401 even where its roles would give a 403 or 404."""
    client, fake = oidc_app("always", AuthError("token is not active"))
    client.app.dependency_overrides[get_principal] = lambda: Principal(  # type: ignore[attr-defined]
        "kc-user", (), "oidc", token="the-token"
    )
    assert client.get("/projects/demo").status_code == 401
    assert fake.tokens == ["the-token"]


@pytest.mark.req("AUTH-10")
def test_always_introspects_auth_me(
    oidc_app: Callable[..., tuple[TestClient, FakeOidc]],
) -> None:
    client, fake = oidc_app("always")
    assert client.get("/auth/me").status_code == 200
    assert fake.tokens == ["the-token"]


@pytest.mark.req("AUTH-10")
def test_always_rejects_a_revoked_token_on_auth_me(
    oidc_app: Callable[..., tuple[TestClient, FakeOidc]],
) -> None:
    client, fake = oidc_app("always", AuthError("token is not active"))
    r = client.get("/auth/me")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"
    assert fake.tokens == ["the-token"]


@pytest.mark.req("AUTH-10")
def test_always_reports_an_identity_provider_outage_on_auth_me(
    oidc_app: Callable[..., tuple[TestClient, FakeOidc]],
) -> None:
    client, _ = oidc_app("always", AuthError("identity provider unavailable", 502))
    assert client.get("/auth/me").status_code == 502


@pytest.mark.req("AUTH-10")
@pytest.mark.parametrize("mode", ["sensitive", "off"])
def test_sensitive_and_off_leave_auth_me_to_local_validation(
    oidc_app: Callable[..., tuple[TestClient, FakeOidc]], mode: str
) -> None:
    client, fake = oidc_app(mode, AuthError("would fail if asked", 502))
    assert client.get("/auth/me").status_code == 200
    assert fake.tokens == []

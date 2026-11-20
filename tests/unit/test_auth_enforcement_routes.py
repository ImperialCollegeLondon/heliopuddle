"""Credential roles and the route table of role enforcement (AUTH-07)."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import select

from heliopuddle.server.api import create_api
from heliopuddle.server.api.deps import requires
from heliopuddle.server.auth.roles import (
    ROLE_ADMIN,
    ROLE_DELETER,
    ROLE_OPERATOR,
    ROLE_READER,
    ROLE_WRITER,
)
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import StorageLocation
from tests.unit import _auth_enforcement_helpers as _fixtures
from tests.unit._auth_enforcement_helpers import ENDPOINTS, STORAGE_ROLE, Act

act_as = _fixtures.act_as
app = _fixtures.app
client = _fixtures.client
config_dir = _fixtures.config_dir


PUBLIC = {"/health", "/version", "/auth/config", "/auth/token"}
"""Routes that need no credentials."""

ANY_TOKEN = {"/auth/me", "/auth/me/password"}
"""Routes any valid token may call (they authenticate through ``get_principal``)."""

DOCS = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
"""FastAPI's own documentation routes."""


def _dependency_calls(dependant: Dependant) -> Iterator[Callable[..., Any]]:
    for sub in dependant.dependencies:
        if sub.call is not None:
            yield sub.call
        yield from _dependency_calls(sub)


def _walk_routes(routes: Iterable[Any], prefix: str = "") -> Iterator[tuple[str, Any]]:
    """Yield ``(path, route)`` for every route, through routers included into the app.

    FastAPI keeps an included router as one object holding the original router, so
    ``app.routes`` alone does not list its routes in recent versions.
    """
    for route in routes:
        original = getattr(route, "original_router", None)
        if original is not None:
            yield from _walk_routes(original.routes, prefix + route.include_context.prefix)
        else:
            yield prefix + getattr(route, "path", ""), route


def _unprotected(app: FastAPI) -> tuple[list[str], int]:
    """The routes that neither carry a ``requires`` dependency nor are listed as open."""
    undeclared: list[str] = []
    seen = 0
    for path, route in _walk_routes(app.routes):
        if path in DOCS:
            continue
        if not isinstance(route, APIRoute):
            undeclared.append(f"{path} (not an APIRoute)")
            continue
        seen += 1
        if path in PUBLIC or path in ANY_TOKEN:
            continue
        if not any(hasattr(call, "hp_requires") for call in _dependency_calls(route.dependant)):
            undeclared.append(f"{','.join(sorted(route.methods or ()))} {path}")
    return undeclared, seen


@pytest.mark.req("AUTH-07", "AUTH-08", "API-03")
@pytest.mark.parametrize("role", [ROLE_READER, ROLE_WRITER, ROLE_DELETER, ROLE_OPERATOR])
@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("active", [True, False])
def test_backend_credentials_require_the_storage_credential_role(
    client: TestClient, act_as: Act, ctx: AppContext, role: str, scoped: bool, active: bool
) -> None:
    secret_url = "memory://user:private-password@store/root?token=private-token"
    cfg = ctx.projects["demo"]
    location = cfg.storage_locations[0].model_copy(
        update={"url": secret_url, "storage_options": {"nested": {"credential": "private-key"}}}
    )
    ctx.projects["demo"] = cfg.model_copy(update={"storage_locations": [location]})
    with ctx.write_session() as session:
        rows = session.scalars(select(StorageLocation)).all()
        for row in rows:
            row.url = secret_url
            row.active = active
    act_as(f"{role}:demo" if scoped else role)
    listed = client.get("/projects/demo/storage-locations")
    assert listed.status_code == 200
    assert all(row["url"] == "" for row in listed.json())
    assert "private-" not in listed.text
    detail = client.get("/projects/demo/storage-locations/primary")
    assert detail.status_code == 403
    assert detail.json()["required_roles"] == [
        STORAGE_ROLE,
        f"{STORAGE_ROLE}:demo",
        ROLE_ADMIN,
    ]
    assert "private-" not in detail.text


@pytest.mark.req("AUTH-07", "AUTH-08", "API-03")
@pytest.mark.parametrize("role", [ROLE_ADMIN, STORAGE_ROLE, f"{STORAGE_ROLE}:demo"])
@pytest.mark.parametrize("active", [True, False])
def test_credential_roles_keep_direct_storage_access(
    client: TestClient, act_as: Act, ctx: AppContext, role: str, active: bool
) -> None:
    cfg = ctx.projects["demo"]
    location = cfg.storage_locations[0].model_copy(
        update={"storage_options": {"nested": {"credential": "private-key"}}}
    )
    ctx.projects["demo"] = cfg.model_copy(update={"storage_locations": [location]})
    if not active:
        with ctx.write_session() as session:
            row = session.scalar(
                select(StorageLocation).where(
                    StorageLocation.project.has(name="demo"), StorageLocation.name == "primary"
                )
            )
            assert row is not None
            row.active = False
    act_as(role)
    listed = client.get("/projects/demo/storage-locations")
    assert listed.status_code == 200
    assert listed.json()[0]["url"] == location.url
    detail = client.get("/projects/demo/storage-locations/primary")
    assert detail.status_code == 200
    assert detail.json()["storage_options"] == (location.storage_options if active else {})


@pytest.mark.req("AUTH-07", "AUTH-08", "API-03")
@pytest.mark.parametrize("other_reader", [False, True])
def test_scoped_credentials_do_not_leak_other_project_urls_or_options(
    client: TestClient, act_as: Act, other_reader: bool
) -> None:
    roles = [f"{STORAGE_ROLE}:demo"]
    if other_reader:
        roles.append("hp-reader:other")
    act_as(*roles)
    names = sorted(project["name"] for project in client.get("/projects").json())
    assert names == (["demo", "other"] if other_reader else ["demo"])
    listed = client.get("/projects/other/storage-locations")
    detail = client.get("/projects/other/storage-locations/main")
    if other_reader:
        assert listed.status_code == 200
        assert all(row["url"] == "" for row in listed.json())
        assert detail.status_code == 403
        assert detail.json()["required_roles"] == [
            STORAGE_ROLE,
            f"{STORAGE_ROLE}:other",
            ROLE_ADMIN,
        ]
    else:
        assert listed.status_code == detail.status_code == 404
        assert detail.json() == {"detail": "not found"}
    assert "storage_options" not in detail.json()


@pytest.mark.req("AUTH-07")
def test_every_route_declares_a_role_or_is_explicitly_public(app: FastAPI) -> None:
    undeclared, seen = _unprotected(app)
    assert not undeclared, f"routes with no `requires(...)` dependency: {undeclared}"
    assert seen >= len(ENDPOINTS)  # the walk really found the routes


@pytest.mark.req("AUTH-07")
def test_route_table_test_catches_an_unprotected_route(ctx: AppContext) -> None:
    """A route added without `requires` is reported, one with it is not."""
    application = create_api(ctx)

    @application.get("/leaky")
    def leaky() -> dict[str, str]:  # pragma: no cover - never called
        return {}

    @application.get("/guarded", dependencies=[Depends(requires(ROLE_READER))])
    def guarded() -> dict[str, str]:  # pragma: no cover - never called
        return {}

    undeclared, _ = _unprotected(application)
    assert undeclared == ["GET /leaky"]


@pytest.mark.req("AUTH-07")
def test_public_routes_need_no_credentials(client: TestClient, prefect_healthy: None) -> None:
    assert client.get("/version").status_code == 200
    assert client.get("/health").status_code in (200, 503)

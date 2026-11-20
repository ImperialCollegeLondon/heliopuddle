"""The example Keycloak realm matches the role model (AUTH-17).

The real-Keycloak check is the manual test in ``examples/keycloak/README.md``;
this keeps the importable realm, the compose file and the docs consistent with
the code.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from heliopuddle.server.auth.roles import ALL_ROLES, PROJECT_ROLES, parse_role
from heliopuddle.settings import Settings

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "keycloak"
ROOT = EXAMPLE.parents[1]


@pytest.fixture(scope="module")
def realm() -> dict[str, Any]:
    return json.loads((EXAMPLE / "realm-heliopuddle.json").read_text())


def realm_roles(realm: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {r["name"]: r for r in realm["roles"]["realm"]}


@pytest.mark.req("AUTH-17")
def test_realm_defines_every_global_role_and_example_scoped_roles(realm: dict[str, Any]) -> None:
    roles = realm_roles(realm)
    assert set(ALL_ROLES) <= set(roles)
    assert "hp-client-storage-read-write" in roles
    assert "hp-client-storage-read-write:demo" in roles
    scoped = [r for r in roles if ":" in r]
    assert scoped, "the example should show project-scoped roles"
    for name in scoped:
        parsed = parse_role(name)
        assert parsed is not None and parsed[0] in PROJECT_ROLES and parsed[1]


@pytest.mark.req("AUTH-17")
def test_realm_has_a_composite_bundle_of_known_roles(realm: dict[str, Any]) -> None:
    composites = [r for r in realm["roles"]["realm"] if r.get("composite")]
    assert composites
    for role in composites:
        assert set(role["composites"]["realm"]) <= set(realm_roles(realm))


@pytest.mark.req("AUTH-17")
def test_realm_client_is_confidential_with_the_flows_hp_uses(realm: dict[str, Any]) -> None:
    (client,) = [c for c in realm["clients"] if c["clientId"] == "heliopuddle"]
    assert client["publicClient"] is False
    assert client["secret"]
    assert client["standardFlowEnabled"] and client["directAccessGrantsEnabled"]
    assert client["serviceAccountsEnabled"]
    assert any("localhost:8080" in uri for uri in client["redirectUris"])
    mappers = {m["protocolMapper"]: m for m in client["protocolMappers"]}
    audience = mappers["oidc-audience-mapper"]
    assert audience["config"]["included.client.audience"] == "heliopuddle"
    assert audience["config"]["access.token.claim"] == "true"


@pytest.mark.req("AUTH-17")
def test_realm_users_hold_only_defined_roles_and_have_demo_passwords(
    realm: dict[str, Any],
) -> None:
    known = set(realm_roles(realm))
    users = {u["username"]: u for u in realm["users"]}
    assert {"admin-demo", "reader-demo", "writer-demo", "demo-only"} <= set(users)
    for user in users.values():
        assert set(user.get("realmRoles", [])) <= known
        if not user["username"].startswith("service-account-"):
            (credential,) = user["credentials"]
            assert credential["type"] == "password" and credential["temporary"] is False
    assert users["demo-only"]["realmRoles"] == ["hp-writer:demo"]
    assert users["admin-demo"]["realmRoles"] == ["hp-admin"]


@pytest.mark.req("AUTH-17")
def test_compose_imports_the_realm_and_example_projects_load() -> None:
    compose = yaml.safe_load((EXAMPLE / "docker-compose.yml").read_text())
    keycloak = compose["services"]["keycloak"]
    assert "--import-realm" in keycloak["command"]
    assert any("realm-heliopuddle.json" in str(v) for v in keycloak["volumes"])
    names = {
        yaml.safe_load(p.read_text())["project"] for p in (EXAMPLE / "projects").glob("*.yaml")
    }
    assert names == {"demo", "other"}


@pytest.mark.req("AUTH-17")
def test_docs_list_every_auth_setting_and_the_upgrade_note() -> None:
    deployment = (ROOT / "docs" / "design" / "deployment.md").read_text()
    architecture = (ROOT / "docs" / "design" / "architecture.md").read_text()
    for field in Settings.model_fields:
        if field.startswith(("auth_", "oidc_", "admin_")):
            variable = f"HELIOPUDDLE_{field.upper()}"
            assert variable in architecture or variable in deployment, variable
    assert "HELIOPUDDLE_AUTH_MODE=header" in deployment  # the upgrade note
    readme = (EXAMPLE / "README.md").read_text()
    for role in ALL_ROLES:
        assert role in readme
    assert "six global roles" in readme
    assert "hp-client-storage-read-write:demo" in readme
    assert "backend credentials" in readme
    assert "independently" in readme


@pytest.mark.req("AUTH-17")
def test_compose_starts_hp_only_once_keycloak_serves_the_discovery_document() -> None:
    compose = yaml.safe_load((EXAMPLE / "docker-compose.yml").read_text())
    check = " ".join(compose["services"]["keycloak"]["healthcheck"]["test"])
    assert "/realms/heliopuddle/.well-known/openid-configuration" in check
    assert "200" in check
    hp = compose["services"]["hp"]
    # hp's OIDC startup is fail-fast: it must not start before discovery works.
    assert hp["depends_on"]["keycloak"]["condition"] == "service_healthy"
    assert hp["restart"] == "on-failure"

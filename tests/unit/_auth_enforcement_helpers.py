"""Shared app, world and endpoint table for the role enforcement tests."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from heliopuddle.server.api import create_api
from heliopuddle.server.api.deps import get_principal
from heliopuddle.server.auth.principal import Principal
from heliopuddle.server.auth.roles import (
    ROLE_ADMIN,
    ROLE_DELETER,
    ROLE_OPERATOR,
    ROLE_READER,
    ROLE_WRITER,
)
from heliopuddle.server.context import AppContext

OTHER_YAML = """\
project: other
storage_locations:
  - name: main
    url: file://{store}
    storage_options:
      auto_mkdir: true
"""


STORAGE_ROLE = "hp-client-storage-read-write"


PROJECT_ROLE_CASES = [ROLE_READER, ROLE_WRITER, ROLE_DELETER, ROLE_OPERATOR, STORAGE_ROLE]


@pytest.fixture
def config_dir(config_dir: Path, tmp_path: Path) -> Path:
    """Two projects: ``demo`` and ``other``."""
    other_store = tmp_path / "other-store"
    other_store.mkdir()
    (config_dir / "other.yaml").write_text(OTHER_YAML.format(store=other_store))
    return config_dir


@pytest.fixture
def app(ctx: AppContext) -> Iterator[FastAPI]:
    application = create_api(ctx)
    yield application
    application.dependency_overrides.clear()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


Act = Callable[..., Principal]


@pytest.fixture
def act_as(app: FastAPI) -> Act:
    """Make the next requests come from a caller holding ``roles``."""

    def act(*roles: str, username: str = "tester") -> Principal:
        principal = Principal(username, tuple(roles), "local", token="tok")
        app.dependency_overrides[get_principal] = lambda: principal
        return principal

    return act


@dataclass
class World:
    """Ids of rows in ``demo`` and ``other``."""

    demo_file: int
    other_file: int
    demo_series: int
    other_series: int
    demo_token: str
    other_token: str


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


@pytest.fixture
def world(client: TestClient, act_as: Act, store: Path, tmp_path: Path) -> World:
    """One file and one open upload in each project, made by an admin."""
    act_as(ROLE_ADMIN)
    ids: dict[str, Any] = {}
    for project, root in (("demo", store), ("other", tmp_path / "other-store")):
        data = f"{project}-data".encode()
        (root / "f.txt").write_bytes(data)
        r = client.post(
            f"/projects/{project}/files",
            json={"path": "f.txt", "hash": _md5(data), "size": len(data)},
        )
        assert r.status_code == 201, r.text
        ids[f"{project}_file"] = r.json()["id"]
        ids[f"{project}_series"] = r.json()["series_id"]
        up = client.post(
            f"/projects/{project}/uploads",
            json={"path": "u.txt", "hash": _md5(b"u"), "size": 1},
        )
        assert up.status_code == 200, up.text
        ids[f"{project}_token"] = up.json()["token"]
    return World(**ids)


@dataclass(frozen=True)
class Endpoint:
    """One route as a caller sees it, the role it needs, and the project it is about."""

    name: str
    method: str
    path: str  # may contain {project}-style placeholders filled from the World
    needed: str | None
    project: str
    body: Any = None


def _endpoints() -> list[Endpoint]:
    r, w, d, o = ROLE_READER, ROLE_WRITER, ROLE_DELETER, ROLE_OPERATOR
    new_file = {"path": "n.txt", "hash": _md5(b"n"), "size": 1}
    return [
        Endpoint("project", "GET", "/projects/{p}", r, "demo"),
        Endpoint("locations", "GET", "/projects/{p}/storage-locations", r, "demo"),
        Endpoint(
            "location", "GET", "/projects/{p}/storage-locations/primary", STORAGE_ROLE, "demo"
        ),
        Endpoint("rules", "GET", "/projects/{p}/rules", r, "demo"),
        Endpoint("rule", "GET", "/projects/{p}/rules/missing", r, "demo"),
        Endpoint("files", "GET", "/projects/{p}/files", r, "demo"),
        Endpoint("summary", "GET", "/projects/{p}/summary", r, "demo"),
        Endpoint("by-path", "GET", "/projects/{p}/files/by-path?path=f.txt", r, "demo"),
        Endpoint("check", "POST", "/projects/{p}/files/check", r, "demo", new_file),
        Endpoint("file", "GET", "/files/{file}", r, "demo"),
        Endpoint("file-content", "GET", "/files/{file}/content", r, "demo"),
        Endpoint("versions", "GET", "/series/{series}/versions", r, "demo"),
        Endpoint("register", "POST", "/projects/{p}/files", w, "demo", new_file),
        Endpoint("reserve", "POST", "/projects/{p}/uploads", w, "demo", new_file),
        Endpoint("upload-content", "PUT", "/uploads/{token}/content", w, "demo"),
        Endpoint("renew", "POST", "/uploads/{token}/renew", w, "demo"),
        Endpoint("complete", "POST", "/uploads/{token}/complete", w, "demo"),
        Endpoint("abort", "DELETE", "/uploads/{token}", w, "demo"),
        Endpoint("metadata", "PATCH", "/files/{file}/metadata", w, "demo", {"a": 1}),
        Endpoint(
            "content-date", "PATCH", "/files/{file}/content-date", w, "demo", {"content_date": None}
        ),
        Endpoint(
            "rules-ignore", "PATCH", "/files/{file}/rules-ignore", w, "demo", {"ignored": True}
        ),
        Endpoint("accept", "POST", "/files/{file}/accept", w, "demo"),
        Endpoint("delete", "DELETE", "/files/{file}", d, "demo"),
        Endpoint("run", "POST", "/projects/{p}/runs", o, "demo", {}),
        Endpoint("rule-enable", "POST", "/projects/{p}/rules/missing/enable", o, "demo"),
        Endpoint("rule-disable", "POST", "/projects/{p}/rules/missing/disable", o, "demo"),
        Endpoint("rule-trigger", "POST", "/projects/{p}/rules/missing/trigger", o, "demo"),
    ]


ENDPOINTS = _endpoints()


def _call(client: TestClient, e: Endpoint, world: World, project: str | None = None) -> Any:
    """Call ``e`` against ``project`` (default: its own), using that project's rows."""
    project = project or e.project
    path = e.path.format(
        p=project,
        file=getattr(world, f"{project}_file"),
        series=getattr(world, f"{project}_series"),
        token=getattr(world, f"{project}_token"),
    )
    return client.request(e.method, path, json=e.body)


def _denied(response: Any) -> bool:
    """Whether the auth layer (not the route) turned the request away."""
    if response.status_code in (401, 403):
        return True
    return response.status_code == 404 and response.json().get("detail") == "not found"


def _roles_allowed(needed: str) -> set[str]:
    allowed = {ROLE_ADMIN, needed}
    if needed == ROLE_READER:
        allowed |= {ROLE_WRITER, ROLE_DELETER, ROLE_OPERATOR, STORAGE_ROLE}
    return allowed

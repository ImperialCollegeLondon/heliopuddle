"""Series key namespace (API-13) and invalid version values on register (DEP-12)."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from heliopuddle.config.models import VersionRule
from heliopuddle.server import models
from heliopuddle.server.api import create_api
from heliopuddle.server.api.files import register as register_api
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import Series
from heliopuddle.server.models.projects import get_project
from heliopuddle.versioning import InvalidVersionError

DRAFT_RULE = r"""
versioning:
  - paths: ["drafts/**"]
    extraction_pattern: '_(?P<version>[a-z]+)\.txt$'
  - paths: ["**"]
    extraction_pattern: '_[vV](?P<version>\d{1,5})\.[^./]+$'
"""


@pytest.fixture
def config_dir(config_dir: Path) -> Path:
    demo = config_dir / "demo.yaml"
    demo.write_text(demo.read_text() + DRAFT_RULE)
    return config_dir


@pytest.fixture
def client(ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(register_api.verify_file, "fn", lambda _id: None)
    with TestClient(create_api(ctx)) as c:
        yield c


def _register(client: TestClient, path: str) -> dict:
    r = client.post(
        "/projects/demo/files",
        json={"path": path, "hash": hashlib.md5(b"x").hexdigest(), "size": 1},
    )
    assert r.status_code == 201, r.text
    return r.json()


@pytest.mark.req("API-13")
def test_literal_path_and_generated_key_are_separate_series(
    ctx: AppContext, client: TestClient
) -> None:
    generated = _register(client, "x_v1.dat")
    literal = _register(client, "x_v{v}.dat")
    assert generated["series_id"] != literal["series_id"]
    # Responses show the key without its namespace prefix.
    assert generated["series_key"] == literal["series_key"] == "x_v{v}.dat"
    versions = client.get(f"/series/{literal['series_id']}/versions").json()
    assert versions["series_key"] == "x_v{v}.dat"
    assert [f["path"] for f in versions["items"]] == ["x_v{v}.dat"]
    # Both are latest in their own series.
    assert generated["is_latest"] and literal["is_latest"]
    with ctx.sessionmaker() as session:
        assert sorted(session.scalars(select(Series.series_key))) == [
            "p:x_v{v}.dat",
            "v:x_v{v}.dat",
        ]


@pytest.mark.req("DEP-12")
def test_non_integer_version_is_422_on_register_and_reserve(client: TestClient) -> None:
    body = {"path": "drafts/x_draft.txt", "hash": hashlib.md5(b"x").hexdigest(), "size": 1}
    for url in ("/projects/demo/files", "/projects/demo/uploads"):
        r = client.post(url, json=body)
        assert r.status_code == 422, r.text
        (error,) = r.json()["detail"]
        assert error["ctx"] == {"rule_index": 0, "group": "version", "value": "draft"}
        assert "rule 0" in error["msg"]
    # Nothing was recorded.
    assert client.get("/projects/demo/files").json()["total"] == 0


@pytest.mark.req("API-20", "DEP-12")
def test_an_override_replaces_an_invalid_extracted_version(client: TestClient) -> None:
    for url, path in (
        ("/projects/demo/files", "drafts/x_draft.txt"),
        ("/projects/demo/uploads", "drafts/y_draft.txt"),
    ):
        body = {"path": path, "hash": hashlib.md5(b"x").hexdigest(), "size": 1}
        r = client.post(url, json=body | {"version_number": 1})
        assert r.status_code in (200, 201), r.text
        record = r.json().get("file", r.json())
        assert (record["version_number"], record["version_text"]) == (1, None)
        # The series still comes from the rule that matched the path.
        assert record["series_key"] == path.replace("_draft", "_{v}")


@pytest.mark.req("API-22", "DEP-12")
def test_a_live_file_keeps_its_version_after_its_rule_turns_invalid(
    ctx: AppContext, client: TestClient
) -> None:
    first = _register(client, "drafts/y_v1.txt")  # the second rule: number 1
    assert first["version_number"] == 1
    changed = [VersionRule(paths=["**"], extraction_pattern=r"_(?P<version>[a-z0-9]+)\.txt$")]
    with ctx.write_session() as session:
        project = get_project(session, "demo")
        assert project is not None
        _series, file, _info = models.lock_path(session, project.id, "drafts/y_v1.txt", changed)
        assert file is not None and file.id == first["id"]
        assert (file.version_number, file.version_text) == (1, None)
        # A new path under the changed rule is still rejected.
        with pytest.raises(InvalidVersionError):
            models.lock_path(session, project.id, "drafts/z_v2.txt", changed)

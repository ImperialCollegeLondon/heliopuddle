"""Shared fixtures and helpers for the files API tests."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from heliopuddle.server.api import create_api
from heliopuddle.server.api.files import register as register_api
from heliopuddle.server.context import AppContext

VERSIONING_YAML = r"""
versioning:
  - paths: ["cal/**"]
    version_type: text
    extraction_pattern: '_v(?P<major>\d{1,3})\.(?P<minor>\d{1,4})\.(?P<ext>[^./]+)$'
    version_text: '{major:0>3}.{minor:0>4}'
  - paths: ["sci/**"]
    extraction_pattern: '_(?P<date>\d{8})_v(?P<version>\d+)\.'
  - paths: ["**"]
    extraction_pattern: '_[vV](?P<version>\d{1,5})\.[^./]+$'
"""


@pytest.fixture
def config_dir(config_dir: Path) -> Path:
    """The demo project with version rules."""
    demo = config_dir / "demo.yaml"
    demo.write_text(demo.read_text() + VERSIONING_YAML)
    return config_dir


@pytest.fixture
def client(ctx: AppContext) -> Iterator[TestClient]:
    with TestClient(create_api(ctx)) as c:
        yield c


@pytest.fixture
def no_verify(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Replace the inline verify with a spy that records the copy ids."""
    calls: list[int] = []
    monkeypatch.setattr(register_api.verify_file, "fn", calls.append)
    return calls


def _put(store: Path, path: str, data: bytes, **extra: Any) -> dict[str, Any]:
    target = store / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return {"path": path, "hash": hashlib.md5(data).hexdigest(), "size": len(data)} | extra


def _add(client: TestClient, store: Path, path: str, data: bytes = b"x", **extra: Any) -> dict:
    r = client.post("/projects/demo/files", json=_put(store, path, data, **extra))
    assert r.status_code in (200, 201), r.text
    return r.json()

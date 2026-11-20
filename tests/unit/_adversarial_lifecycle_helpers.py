"""Shared helpers and fixtures for the split adv_lifecycle tests."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from heliopuddle.server.context import AppContext
from heliopuddle.storage import StorageLocation
from tests.adversarial import (
    client,
    clock,  # noqa: F401 - fixture
    config_dir,  # noqa: F401 - fixture
    protocol,  # noqa: F401 - fixture
    put,
)
from tests.lifecycle import copies, md5

_FIXTURES = (client,)  # referenced so ruff sees the imports used

OLD = b"original content"


NEW = b"the new content, longer"


PATH = "p/data_v1.dat"


def reserve(client: TestClient, data: bytes, path: str = PATH, **extra: Any) -> Any:
    return client.post(
        "/projects/demo/uploads",
        json={"path": path, "hash": md5(data), "size": len(data)} | extra,
    )


def upload(
    client: TestClient, loc: StorageLocation, data: bytes, path: str = PATH, **extra: Any
) -> dict[str, Any]:
    res = reserve(client, data, path, **extra)
    assert res.status_code == 200, res.text
    body = res.json()
    if body["mode"] == "unchanged":
        return body["file"]
    put(loc, body["write_path"], data)
    done = client.post(f"/uploads/{body['token']}/complete")
    assert done.status_code == 200, done.text
    return done.json()


def rows(ctx: AppContext, location: str = "primary") -> list[Any]:
    return [c for c in copies(ctx) if c.storage_location == location]


@pytest.fixture
def fast_index(ctx: AppContext) -> None:
    ctx.projects["demo"].index.settle_time = timedelta(0)


def _hooked_info(real: Any, hook: Any) -> Any:
    """``StorageLocation.info`` that runs ``hook`` after the first answer for a path."""
    state = {"busy": False}

    def info(self: StorageLocation, path: str) -> Any:
        result = real(self, path)
        if not state["busy"] and self is not None:
            state["busy"] = True
            try:
                hook(self, path)
            finally:
                state["busy"] = False
        return result

    return info

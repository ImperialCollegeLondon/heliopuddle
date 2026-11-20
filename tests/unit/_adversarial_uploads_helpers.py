"""Shared helpers and fixtures for the split adv_uploads tests."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

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


NEWER = b"a third version of the content"


PATH = "p/data_v1.dat"


def reserve(client: TestClient, data: bytes, path: str = PATH, **extra: Any) -> Any:
    return client.post(
        "/projects/demo/uploads",
        json={"path": path, "hash": md5(data), "size": len(data)} | extra,
    )


def upload(
    client: TestClient, loc: StorageLocation, data: bytes, path: str = PATH, **extra: Any
) -> dict[str, Any]:
    """Reserve, write and complete; returns the record."""
    res = reserve(client, data, path, **extra)
    assert res.status_code == 200, res.text
    body = res.json()
    if body["mode"] == "unchanged":
        return body["file"]
    put(loc, body["write_path"], data)
    done = client.post(f"/uploads/{body['token']}/complete")
    assert done.status_code == 200, done.text
    return done.json()


def only_copy(ctx: AppContext, location: str = "primary") -> Any:
    (row,) = [c for c in copies(ctx) if c.storage_location == location]
    return row


def _hooks(*fns: Callable[[], None]) -> None:  # pragma: no cover - documentation helper
    for fn in fns:
        fn()

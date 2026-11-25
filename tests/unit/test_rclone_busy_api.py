"""A busy rclone config answers 503 rclone_busy with Retry-After on every path (STO-20).

``RcloneBusyError`` is a ``TimeoutError`` and so an ``OSError``: the content,
preview and upload services and routers must pass it on to the global handler
instead of turning it into a generic storage failure.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from heliopuddle.storage.location import StorageLocation
from heliopuddle.storage.rclone import RcloneBusyError
from tests.local_auth import add_user
from tests.unit.test_file_content_api import api, reserve, token_for

__all__ = ["api"]  # the fixture, re-used from the content API tests

BUSY = RcloneBusyError("rclone config block 'sp' is in use")


def _assert_busy(response: object) -> None:
    from httpx import Response

    assert isinstance(response, Response)
    assert response.status_code == 503, response.text
    assert response.json()["reason"] == "rclone_busy"
    assert int(response.headers["Retry-After"]) >= 1


def _stored_file(api: TestClient) -> int:
    token = reserve(api)["token"]
    assert api.put(f"/uploads/{token}/content", content=b"content").status_code == 204
    record = api.post(f"/uploads/{token}/complete").json()
    return int(record["id"])


@pytest.mark.req("STO-20")
@pytest.mark.parametrize("route", ["content", "preview"])
def test_busy_config_on_read_is_503(
    api: TestClient, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    file_id = _stored_file(api)

    def busy(*args: object, **kwargs: object) -> None:
        raise BUSY

    monkeypatch.setattr(StorageLocation, "open_read", busy)
    monkeypatch.setattr(StorageLocation, "info", busy)
    _assert_busy(api.get(f"/files/{file_id}/{route}"))


@pytest.mark.req("STO-20")
def test_busy_config_on_upload_is_503(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    token = reserve(api)["token"]

    def busy(*args: object, **kwargs: object) -> None:
        raise BUSY

    monkeypatch.setattr(StorageLocation, "write", busy)
    _assert_busy(api.put(f"/uploads/{token}/content", content=b"content"))


@pytest.mark.req("STO-20")
def test_busy_config_on_delete_is_503(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from heliopuddle.server.api.files import delete as delete_route

    file_id = _stored_file(api)

    class Busy:
        @staticmethod
        def fn(*args: object, **kwargs: object) -> None:
            raise BUSY

    monkeypatch.setattr(delete_route, "delete_file", Busy)
    add_user(api.app.state.ctx, "deleter", ["hp-deleter:demo"])  # type: ignore[union-attr]
    _assert_busy(api.delete(f"/files/{file_id}", headers=token_for(api, "deleter")))

"""Shared fake server and fixtures for the client upload tests."""

from __future__ import annotations

import email.utils
import hashlib
import json
import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import fsspec
import httpx
import pytest

from heliopuddle.client import (
    HelioPuddleClient,
)
from heliopuddle.storage import StorageLocation
from heliopuddle.storage.location import staging_path

API = "http://fake/api/v1"


PROJECT = "demo"


def _record(file_id: int, path: str, data_hash: str, size: int, project: str = PROJECT) -> dict:
    now = datetime.now(UTC).isoformat()
    return {
        "id": file_id,
        "project": project,
        "path": path,
        "name": path.rsplit("/", 1)[-1],
        "series_id": file_id,
        "size": size,
        "hash": data_hash,
        "hash_algo": "md5",
        "is_latest": True,
        "created_at": now,
        "updated_at": now,
        "storage_locations": [
            {"storage_location": "primary", "state": "available", "updated_at": now}
        ],
    }


class FakeServer:
    """The upload protocol, scripted.

    Attributes:
        mode: the reservation answer: ``new``, ``overwrite``, ``unchanged``,
            ``conflict`` or ``busy``.
        lease: seconds from now to each deadline handed out.
        renew_status: status code renews answer with.
        complete_status: status code complete answers with.
        log: every request (and, through the spy, every store write) in order.
    """

    def __init__(self, url: str) -> None:
        self.url = url
        self.store = StorageLocation(url)
        self.mode = "new"
        self.lease = 3600.0
        self.renew_status = 200
        self.complete_status = 200
        self.retry_after = "7"
        self.log: list[str] = []
        self.reservations: dict[str, dict[str, Any]] = {}
        self.files: dict[int, dict[str, Any]] = {}
        self.requests: list[dict[str, Any]] = []

    def _deadline(self) -> str:
        return (datetime.now(UTC) + timedelta(seconds=self.lease)).isoformat()

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        method = request.method
        body = json.loads(request.content) if request.content else None
        date = {"date": email.utils.format_datetime(datetime.now(UTC), usegmt=True)}

        if method == "GET" and (m := re.fullmatch(r"/projects/([^/]+)", path)):
            self.log.append("project")
            return httpx.Response(200, json={"name": m[1], "storage_locations": ["primary"]})
        if method == "GET" and re.fullmatch(r"/projects/[^/]+/storage-locations/primary", path):
            self.log.append("storage-location")
            return httpx.Response(
                200,
                json={
                    "name": "primary",
                    "url": self.url,
                    "accept_external_changes": True,
                    "active": True,
                    "create_root": "auto",
                    "storage_options": {},
                },
            )
        if method == "POST" and path == f"/projects/{PROJECT}/uploads":
            self.log.append("reserve")
            self.requests.append(body or {})
            return self._reserve(body or {}, date)
        if m := re.fullmatch(r"/uploads/([^/]+)/renew", path):
            self.log.append("renew")
            if self.renew_status != 200:
                return httpx.Response(self.renew_status, json={"detail": "reservation lost"})
            res = self.reservations[m[1]]
            return httpx.Response(
                200,
                headers=date,
                json={
                    "mode": res["mode"],
                    "token": m[1],
                    "write_path": res["write_path"],
                    "deadline": self._deadline(),
                },
            )
        if m := re.fullmatch(r"/uploads/([^/]+)/complete", path):
            self.log.append("complete")
            if self.complete_status != 200:
                return httpx.Response(self.complete_status, json={"detail": "complete failed"})
            return self._complete(m[1])
        if method == "DELETE" and (m := re.fullmatch(r"/uploads/([^/]+)", path)):
            self.log.append("abort")
            self.reservations.pop(m[1], None)
            return httpx.Response(204)
        if method == "GET" and (m := re.fullmatch(r"/files/(\d+)", path)):
            self.log.append("get-file")
            rec = self.files.get(int(m[1]))
            return httpx.Response(200, json=rec) if rec else httpx.Response(404, json={})
        if method == "POST" and (m := re.fullmatch(r"/files/(\d+)/accept", path)):
            self.log.append("accept")
            return httpx.Response(200, json=self.files[int(m[1])])
        raise AssertionError(f"unexpected request {method} {path}")

    def _reserve(self, body: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
        path = body["path"]
        existing = _record(1, path, "0" * 32, 3)
        if self.mode == "unchanged":
            existing["hash"] = body["hash"]
            return httpx.Response(200, json={"mode": "unchanged", "file": existing})
        if self.mode == "conflict":
            return httpx.Response(409, json={"detail": "different hash", "file": existing})
        if self.mode == "busy":
            return httpx.Response(
                409, headers={"retry-after": self.retry_after}, json={"detail": "busy"}
            )
        token = uuid.uuid4().hex
        write_path = path if self.mode == "new" else staging_path(path, token)
        self.reservations[token] = {**body, "mode": self.mode, "write_path": write_path}
        return httpx.Response(
            201,
            headers=headers,
            json={
                "mode": self.mode,
                "token": token,
                "write_path": write_path,
                "deadline": self._deadline(),
            },
        )

    def _complete(self, token: str) -> httpx.Response:
        res = self.reservations.pop(token, None)
        if res is None:
            return httpx.Response(409, json={"detail": "reservation taken over"})
        if self.store.md5(res["write_path"]) != res["hash"]:
            return httpx.Response(200, json=_record(5, res["path"], "f" * 32, res["size"]))
        if res["write_path"] != res["path"]:
            self.store.move(res["write_path"], res["path"])  # publish (replacing backend)
        rec = _record(5, res["path"], res["hash"], res["size"])
        self.files[5] = rec
        return httpx.Response(200, json=rec)


@pytest.fixture
def server() -> FakeServer:
    root = f"/hp-client-{uuid.uuid4().hex}"
    fsspec.filesystem("memory").mkdir(root)
    return FakeServer(f"memory://{root}")


@pytest.fixture
def writes(server: FakeServer, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Spy on every store write: logged as ``write:<path>`` in the server log."""
    real = StorageLocation.write
    paths: list[str] = []

    def spy(self: StorageLocation, path: str, source: Any) -> None:
        if self is not server.store:  # the client's writes, not the fake server's setup
            server.log.append(f"write:{path}")
            paths.append(path)
        real(self, path, source)

    monkeypatch.setattr(StorageLocation, "write", spy)
    return paths


@pytest.fixture
def hp(server: FakeServer) -> Iterator[HelioPuddleClient]:
    http = httpx.Client(base_url=API, transport=httpx.MockTransport(server.handler))
    with HelioPuddleClient(API, PROJECT, user="alice", http_client=http) as c:
        yield c
    http.close()


def _local(tmp_path: Path, data: bytes, name: str = "f.dat") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()

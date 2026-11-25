"""Shared pieces of the adversarial lifecycle tests (``tests/unit/test_adversarial_*.py``).

A replacing and a non-replacing fake store, a controllable clock, and
:func:`assert_invariants`, which extends :func:`tests.lifecycle.assert_consistent`
with the ownership and ``is_latest`` rules of docs/design/operations.md and
docs/design/data-model.md.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import fsspec
import pytest
from fastapi.testclient import TestClient
from fsspec.implementations.memory import MemoryFileSystem
from sqlalchemy import select

from heliopuddle.server.api import create_api
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import File, FileStorageLocation, Series
from heliopuddle.server.models import transitions as tr
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services.stores import store_for
from heliopuddle.storage import StorageLocation, backends
from tests.lifecycle import assert_consistent, md5

OWNED_STATES = frozenset({FileState.PENDING, FileState.OVERWRITING, FileState.DELETING})
ELIGIBLE = frozenset({FileState.AVAILABLE, FileState.OVERWRITING})

__all__ = [
    "Clock",
    "NonReplacingFS",
    "assert_invariants",
    "client",
    "clock",
    "config_dir",
    "config_for",
    "md5",
    "protocol",
    "put",
    "store",
]


class NonReplacingFS(MemoryFileSystem):
    """A memory filesystem whose rename fails when the destination exists (like SMB)."""

    protocol = ("hpnorepl",)
    store: dict[str, Any] = {}  # noqa: RUF012 - fsspec's own class-level store
    pseudo_dirs = [""]  # noqa: RUF012

    def mv(self, path1: Any, path2: Any, *args: Any, **kwargs: Any) -> None:
        if self.exists(path2):
            raise FileExistsError(path2)
        super().mv(path1, path2, *args, **kwargs)


fsspec.register_implementation("hpnorepl", NonReplacingFS, clobber=True)


class Clock:
    """A controllable clock for ownership decisions (patch ``transitions.clock`` with it)."""

    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def config_for(path: Path, protocol: str) -> Path:
    """Write a config dir with project ``demo`` and two storage locations of ``protocol``."""
    path.mkdir()
    tag = uuid.uuid4().hex
    (path / "demo.yaml").write_text(
        "project: demo\nstorage_locations:\n"
        f"  - name: primary\n    url: '{protocol}://hp-{tag}-a'\n"
        f"  - name: secondary\n    url: '{protocol}://hp-{tag}-b'\n"
    )
    return path


def store(ctx: AppContext, name: str = "primary") -> StorageLocation:
    """The configured storage location ``name`` of ``demo``."""
    return store_for(ctx, "demo", name)


def put(loc: StorageLocation, path: str, data: bytes) -> None:
    """Write ``data`` at ``path`` the way a client or another tool would."""
    loc.fs.pipe(loc.full_path(path), data)


def assert_invariants(ctx: AppContext, *, allow_temp: bool = False) -> None:
    """Store and DB invariants after any interleaving.

    - everything :func:`tests.lifecycle.assert_consistent` checks (an
      ``available`` copy's hash and size are the store's, one live file per
      path, no ownerless ``*.hp-tmp``);
    - a copy holding a token is in an owned state (``pending``,
      ``overwriting`` or ``deleting``): ownership never outlives its state;
    - a soft-deleted file has only ``deleted`` copies;
    - per series, ``is_latest`` marks exactly one file when some live file is
      latest-eligible (``available``/``overwriting`` copy, or ``pending``
      without a token), and no ineligible file.
    """
    assert_consistent(ctx, allow_temp=allow_temp)
    with ctx.sessionmaker() as session:
        for copy in session.scalars(select(FileStorageLocation)):
            if copy.op_token is not None:
                assert copy.state in OWNED_STATES, (
                    f"copy {copy.id} ({copy.file.path}) is {copy.state.value} but still "
                    f"holds a token"
                )
            if copy.file.deleted_at is not None:
                assert copy.state == FileState.DELETED, (
                    f"file {copy.file.path} is soft-deleted but its copy is {copy.state.value}"
                )
        for series in session.scalars(select(Series)):
            files = list(session.scalars(select(File).where(File.series_id == series.id)))
            live = [f for f in files if f.deleted_at is None]
            eligible = {
                f.id
                for f in live
                if any(
                    c.state in ELIGIBLE or (c.state == FileState.PENDING and c.op_token is None)
                    for c in f.copies
                )
            }
            latest = {f.id for f in files if f.is_latest}
            assert latest <= eligible, (series.series_key, latest, eligible)
            if eligible:
                assert len(latest) == 1, (series.series_key, latest, eligible)
                assert series.latest_file_id in latest
            else:
                assert latest == set()
                assert series.latest_file_id is None


# --- fixtures (import them into a test module to use them) ------------------------


@pytest.fixture(params=["memory", "hpnorepl"])
def protocol(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """A replacing (``memory``) and a non-replacing (``hpnorepl``) fake store."""
    monkeypatch.setattr(
        backends,
        "NON_REPLACING_MOVE_PROTOCOLS",
        backends.NON_REPLACING_MOVE_PROTOCOLS | {"hpnorepl"},
    )
    return str(request.param)


@pytest.fixture
def config_dir(tmp_path: Path, protocol: str) -> Path:
    """Project ``demo`` with two storage locations on the fake store."""
    return config_for(tmp_path / "config", protocol)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """Controls the time ownership is judged at."""
    c = Clock()
    monkeypatch.setattr(tr, "clock", c)
    return c


@pytest.fixture
def client(ctx: AppContext) -> Iterator[TestClient]:
    """The real API over the app context."""
    with TestClient(create_api(ctx), headers={"X-Forwarded-User": "alice"}) as c:
        yield c

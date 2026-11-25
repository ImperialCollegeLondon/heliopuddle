"""A block-configured storage location whose resolved root moves is reset (STO-19, SVC-12).

Its configured ``url`` (``prefect-block:<name>``) does not change when the
block's base path does, so the move is detected on open from the
credential-free resolved root stored in ``storage_location.resolved_root``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from heliopuddle.server import models
from heliopuddle.server.api import create_api
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import FileStorageLocation, IndexProgress
from heliopuddle.server.database.orm import StorageLocation as StorageLocationRow
from heliopuddle.server.services import stores
from heliopuddle.storage import credentials as creds
from heliopuddle.storage.credentials import root_identity
from tests.unit.test_storage_credentials import Clock, FakeResolver

YAML = """\
project: demo
storage_locations:
  - name: primary
    url: file://{store}
  - name: offsite
    block: offsite-fs
"""


@pytest.fixture
def config_dir(tmp_path: Path, store: Path) -> Path:
    path = tmp_path / "config"
    path.mkdir()
    (path / "demo.yaml").write_text(YAML.format(store=store))
    return path


@pytest.fixture
def clock() -> Iterator[Clock]:
    c = Clock()
    creds.set_clock(c)
    yield c
    creds.set_clock(None)


@pytest.fixture
def blocks(clock: Clock, tmp_path: Path) -> Iterator[FakeResolver]:
    resolver = FakeResolver()
    resolver.filesystems["offsite-fs"] = (f"file://{tmp_path / 'a'}", {"auto_mkdir": True})
    creds.set_block_resolver(resolver)
    stores._seen_roots.clear()  # pyright: ignore[reportPrivateUsage]
    yield resolver
    creds.set_block_resolver(None)
    stores._seen_roots.clear()  # pyright: ignore[reportPrivateUsage]


def _row(ctx: AppContext) -> StorageLocationRow:
    with ctx.sessionmaker() as session:
        row = models.get_storage_location(session, "demo", "offsite")
        assert row is not None
        session.expunge(row)
        return row


def _copies(ctx: AppContext, sl_id: int) -> list[tuple[str, int]]:
    with ctx.sessionmaker() as session:
        rows = session.scalars(
            select(FileStorageLocation).where(FileStorageLocation.storage_location_id == sl_id)
        ).all()
        return [(str(r.state), r.generation) for r in rows]


@pytest.mark.req("STO-19", "SVC-12")
@pytest.mark.parametrize(
    ("url", "identity"),
    [
        ("sftp://user:pw@host:2222/srv/data/?x=1#f", "sftp://host:2222/srv/data"),
        ("az://container/prefix?sig=secret", "az://container/prefix"),
        ("file:///data/", "file:///data"),
        ("rclone://remote/a b", "rclone://remote/a b"),
    ],
)
def test_root_identity_has_no_credentials(url: str, identity: str) -> None:
    assert root_identity(url) == identity


@pytest.mark.req("STO-19", "SVC-12")
def test_a_moved_block_root_resets_the_copies(
    ctx: AppContext, blocks: FakeResolver, clock: Clock, tmp_path: Path
) -> None:
    (tmp_path / "a").mkdir()
    data = b"offsite data"
    (tmp_path / "a" / "x.dat").write_bytes(data)
    with TestClient(create_api(ctx)) as client:
        created = client.post(
            "/projects/demo/files",
            json={
                "path": "x.dat",
                "hash": hashlib.md5(data).hexdigest(),
                "size": len(data),
                "storage_location": "offsite",
            },
        )
    assert created.status_code == 201, created.text
    row = _row(ctx)
    assert row.resolved_root == f"file://{tmp_path / 'a'}"
    [(state, generation)] = _copies(ctx, row.id)
    assert state == "available"
    with ctx.write_session() as session:
        session.add(IndexProgress(storage_location_id=row.id))

    blocks.filesystems["offsite-fs"] = (f"file://{tmp_path / 'b'}", {})
    clock.now += creds.CACHE_TTL + 1  # the block changed after startup
    stores.store_for(ctx, "demo", "offsite")

    assert _row(ctx).resolved_root == f"file://{tmp_path / 'b'}"
    assert _copies(ctx, row.id) == [("pending", generation + 1)]
    with ctx.sessionmaker() as session:
        assert models.get_index_progress(session, [row.id]) == {}


@pytest.mark.req("STO-19")
def test_an_unchanged_root_writes_nothing(
    ctx: AppContext, blocks: FakeResolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    stores.store_for(ctx, "demo", "offsite")
    writes: list[bool] = []
    real = ctx.write_session

    def counting():  # type: ignore[no-untyped-def]
        writes.append(True)
        return real()

    monkeypatch.setattr(ctx, "write_session", counting)
    for _ in range(3):
        stores.store_for(ctx, "demo", "offsite")
    assert writes == []


@pytest.mark.req("STO-19")
def test_nothing_is_noted_while_the_block_is_missing(ctx: AppContext, blocks: FakeResolver) -> None:
    del blocks.filesystems["offsite-fs"]
    cfg = ctx.storage_location_config("demo", "offsite")
    assert cfg is not None
    stores.note_resolved_root(ctx, "demo", cfg)
    assert _row(ctx).resolved_root is None

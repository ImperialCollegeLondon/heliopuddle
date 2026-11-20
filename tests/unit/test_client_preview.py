"""HelioPuddleClient.preview (CLI-12) against the in-process API."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from heliopuddle.client import FilePreview, HelioPuddleClient, NotFoundError
from heliopuddle.server.api import create_api
from heliopuddle.server.context import AppContext
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services import preview as preview_service
from tests.golden_files import add_file

pytestmark = pytest.mark.req("CLI-12")

API_URL = "http://testserver"


@pytest.fixture
def hp(ctx: AppContext) -> Iterator[HelioPuddleClient]:
    preview_service.cache.clear()
    with (
        TestClient(create_api(ctx)) as http,
        HelioPuddleClient(API_URL, "demo", user="alice", http_client=http) as client,
    ):
        yield client


def put(ctx: AppContext, store: Path, path: str, data: bytes, **kwargs: object) -> int:
    (store / path).write_bytes(data)
    with ctx.write_session() as session:
        return add_file(
            session,
            "demo",
            path,
            copies={"primary": FileState.AVAILABLE},
            size=len(data),
            **kwargs,  # type: ignore[arg-type]
        )


def test_preview_by_path_and_by_id(hp: HelioPuddleClient, ctx: AppContext, store: Path) -> None:
    file_id = put(ctx, store, "a.jsonl", b'{"x": 1}\n{"x": 2}\n')
    by_path = hp.preview("a.jsonl")
    assert isinstance(by_path, FilePreview)
    assert (by_path.file_id, by_path.kind, by_path.format) == (file_id, "table", "jsonl")
    assert by_path.tables[0].rows == [[1], [2]]
    assert hp.preview(file_id) == by_path


def test_preview_of_a_missing_or_deleted_file_raises_not_found(
    hp: HelioPuddleClient, ctx: AppContext, store: Path
) -> None:
    deleted = put(ctx, store, "gone.txt", b"x\n", deleted=True, latest=False)
    with pytest.raises(NotFoundError):
        hp.preview("nothing-here.txt")
    with pytest.raises(NotFoundError):
        hp.preview(deleted)


def test_preview_of_unreadable_content_is_kind_error(
    hp: HelioPuddleClient, ctx: AppContext, store: Path
) -> None:
    put(ctx, store, "bad.parquet", b"not parquet")
    p = hp.preview("bad.parquet")
    assert p.kind == "error"
    assert p.message

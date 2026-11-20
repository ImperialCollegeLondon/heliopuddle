"""HelioPuddleClient M2: idempotent add, conflicts, queries, remove, open/download.

Runs against the real API in-process with ``file://`` storage locations
(CLI-02, CLI-03, CLI-05, CLI-06).
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from heliopuddle.client import (
    ConflictError,
    FileRecord,
    HelioPuddleClient,
    HelioPuddleError,
    IntegrityError,
    NotFoundError,
)
from heliopuddle.server.api import create_api
from heliopuddle.server.api.files import register as register_api
from heliopuddle.server.context import AppContext
from heliopuddle.server.schemas import FileState
from heliopuddle.storage.location import StorageLocation
from tests.upload_api import requires_upload_api

API_URL = "http://testserver"


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


@pytest.fixture
def http(ctx: AppContext) -> Iterator[TestClient]:
    with TestClient(create_api(ctx)) as c:
        yield c


@pytest.fixture
def hp(http: TestClient) -> Iterator[HelioPuddleClient]:
    with HelioPuddleClient(API_URL, "demo", user="alice", http_client=http) as c:
        yield c


@pytest.fixture
def no_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the inline verify (copies stay ``pending``)."""
    monkeypatch.setattr(register_api.verify_file, "fn", lambda _id: None)


@pytest.fixture
def writes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every ``StorageLocation.write`` (the call still goes through)."""
    calls: list[str] = []
    original = StorageLocation.write

    def spy(self: StorageLocation, path: str, source: Any) -> None:
        calls.append(path)
        original(self, path, source)

    monkeypatch.setattr(StorageLocation, "write", spy)
    return calls


@pytest.fixture
def registers(http: TestClient, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every upload completion (``POST /uploads/{token}/complete``)."""
    calls: list[str] = []
    original = http.request

    def spy(method: str, url: Any, *args: Any, **kwargs: Any) -> Any:
        if method == "POST" and str(url).endswith("/complete"):
            calls.append(str(url))
        return original(method, url, *args, **kwargs)

    monkeypatch.setattr(http, "request", spy)
    return calls


def _local(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / "local" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _set_state(ctx: AppContext, file_id: int, state: FileState) -> None:
    """Force the state of a file's copies (stand-in for a finished verify)."""
    from sqlalchemy import text

    with ctx.engine.begin() as conn:
        conn.execute(
            text("UPDATE file_storage_location SET state = :s WHERE file_id = :f"),
            {"s": state.value, "f": file_id},
        )


@pytest.fixture
def golden(hp: HelioPuddleClient, tmp_path: Path, ctx: AppContext) -> dict[str, FileRecord]:
    """A versioned golden set, all copies ``available``.

    Series ``x``: v001, v002 (latest); series ``y``: v1, v2 (no zero padding);
    an unversioned ``notes.txt``; ``sub/x_v003.csv`` (a different series).
    """
    files = {
        "x_v001.csv": (b"x1", datetime(2024, 1, 1, tzinfo=UTC)),
        "x_v002.csv": (b"x2", datetime(2024, 1, 2, tzinfo=UTC)),
        "y_v1.csv": (b"y10", datetime(2024, 2, 1, tzinfo=UTC)),
        "y_v2.csv": (b"y20", datetime(2024, 2, 2, tzinfo=UTC)),
        "notes.txt": (b"n", None),
        "sub/x_v003.csv": (b"sx3", datetime(2024, 3, 1, tzinfo=UTC)),
    }
    out: dict[str, FileRecord] = {}
    for path, (data, content_date) in files.items():
        rec = hp.add_file(
            _local(tmp_path, path.replace("/", "_"), data), path, content_date=content_date
        )
        _set_state(ctx, rec.id, FileState.AVAILABLE)
        out[path] = hp.get_file(rec.id)
    return out


# --- CLI-02: idempotent re-add ----------------------------------------------


@requires_upload_api
@pytest.mark.req("CLI-02")
def test_readd_identical_available_file_skips_write_and_register(
    hp: HelioPuddleClient,
    tmp_path: Path,
    store: Path,
    writes: list[str],
    registers: list[str],
) -> None:
    local = _local(tmp_path, "x_v001.csv", b"same")
    first = hp.add_file(local, "x_v001.csv")
    assert first.state == FileState.AVAILABLE
    assert (writes, len(registers)) == (["x_v001.csv"], 1)

    target = store / "x_v001.csv"
    os.utime(target, (1_000_000, 1_000_000))
    mtime = target.stat().st_mtime

    again = hp.add_file(local, "x_v001.csv", metadata={"ignored": True})
    assert again.id == first.id
    assert again.hash == first.hash
    assert (writes, len(registers)) == (["x_v001.csv"], 1)
    assert target.stat().st_mtime == mtime


@requires_upload_api
@pytest.mark.req("CLI-02")
@pytest.mark.usefixtures("no_verify")
def test_readd_identical_but_not_available_writes_and_registers(
    hp: HelioPuddleClient,
    tmp_path: Path,
    ctx: AppContext,
    writes: list[str],
    registers: list[str],
) -> None:
    local = _local(tmp_path, "a.dat", b"a")
    first = hp.add_file(local, "a.dat")
    _set_state(ctx, first.id, FileState.UNKNOWN)  # same hash, but not available
    again = hp.add_file(local, "a.dat")
    assert again.id == first.id
    # An object already exists at a.dat (the unknown copy): the second write is staged
    # and published, never written over it.
    assert writes[0] == "a.dat"
    assert len(writes) == 2
    assert writes[1].startswith("a.dat.") and writes[1].endswith(".hp-tmp")
    assert len(registers) == 2


@requires_upload_api
@pytest.mark.req("CLI-02")
@pytest.mark.usefixtures("no_verify")
def test_readd_available_elsewhere_still_writes_target_location(
    hp: HelioPuddleClient, tmp_path: Path, ctx: AppContext, writes: list[str]
) -> None:
    local = _local(tmp_path, "a.dat", b"a")
    first = hp.add_file(local, "a.dat")
    _set_state(ctx, first.id, FileState.AVAILABLE)
    # Same hash, but no copy in "secondary": the content must be transferred.
    hp.add_file(local, "a.dat", storage_location="secondary")
    assert writes == ["a.dat", "a.dat"]
    assert (tmp_path / "store2" / "a.dat").read_bytes() == b"a"


# --- CLI-05: conflicts and overwrite ----------------------------------------


@requires_upload_api
@pytest.mark.req("CLI-05")
@pytest.mark.usefixtures("no_verify")
def test_conflict_raises_before_touching_store(
    hp: HelioPuddleClient,
    tmp_path: Path,
    store: Path,
    writes: list[str],
    registers: list[str],
) -> None:
    first = hp.add_file(_local(tmp_path, "old", b"old"), "x_v002.csv")
    with pytest.raises(ConflictError) as e:
        hp.add_file(_local(tmp_path, "new", b"new"), "x_v002.csv")
    err = e.value
    assert isinstance(err, HelioPuddleError)
    assert err.status_code == 409
    assert err.file.id == first.id
    assert err.file.hash == _md5(b"old")
    assert "x_v002.csv" in str(err.detail)
    assert writes == ["x_v002.csv"]
    assert len(registers) == 1
    assert (store / "x_v002.csv").read_bytes() == b"old"
    assert sorted(p.name for p in store.iterdir()) == ["x_v002.csv"]


@requires_upload_api
@pytest.mark.req("CLI-05")
def test_overwrite_replaces_content_and_hash(
    hp: HelioPuddleClient, tmp_path: Path, store: Path
) -> None:
    first = hp.add_file(_local(tmp_path, "old", b"old"), "x_v002.csv")
    rec = hp.add_file(_local(tmp_path, "new", b"new"), "x_v002.csv", overwrite=True)
    assert rec.id == first.id
    assert rec.hash == _md5(b"new")
    assert rec.size == 3
    assert rec.state == FileState.AVAILABLE
    assert rec.updated_by == "alice"
    assert (store / "x_v002.csv").read_bytes() == b"new"


@requires_upload_api
@pytest.mark.req("CLI-05")
@pytest.mark.usefixtures("no_verify")
def test_check(hp: HelioPuddleClient, tmp_path: Path) -> None:
    assert not hp.check("c.dat", _md5(b"c")).exists
    rec = hp.add_file(_local(tmp_path, "c", b"c"), "c.dat")
    same = hp.check("c.dat", _md5(b"c"))
    assert (same.exists, same.hash_matches) == (True, True)
    assert same.file is not None and same.file.id == rec.id
    other = hp.check("c.dat", _md5(b"d"))
    assert (other.exists, other.hash_matches) == (True, False)


# --- CLI-03: queries, remove, metadata --------------------------------------


@requires_upload_api
@pytest.mark.req("CLI-03")
@pytest.mark.usefixtures("no_verify")
def test_find_filters(hp: HelioPuddleClient, golden: dict[str, FileRecord]) -> None:
    def paths(**kwargs: Any) -> list[str]:
        return [f.path for f in hp.find(order="path", **kwargs).items]

    assert paths() == sorted(golden)
    assert hp.find().total == 6
    assert hp.find().items[0].path == "sub/x_v003.csv"  # newest first
    assert paths(path="x_*.csv") == ["x_v001.csv", "x_v002.csv"]
    assert paths(path="**/x_*.csv") == ["sub/x_v003.csv", "x_v001.csv", "x_v002.csv"]
    assert paths(latest_only=True) == [
        "notes.txt",
        "sub/x_v003.csv",
        "x_v002.csv",
        "y_v2.csv",
    ]
    assert paths(series_id=golden["x_v001.csv"].series_id) == ["x_v001.csv", "x_v002.csv"]
    assert paths(state=FileState.AVAILABLE) == sorted(golden)
    assert paths(state=FileState.PENDING) == []
    assert paths(storage_location="primary") == sorted(golden)
    assert paths(storage_location="secondary") == []
    assert paths(
        content_date_from=datetime(2024, 1, 2, tzinfo=UTC),
        content_date_to=datetime(2024, 2, 1, tzinfo=UTC),
    ) == ["x_v002.csv", "y_v1.csv"]
    page = hp.find(order="path", limit=2, offset=2)
    assert (page.total, page.limit, page.offset) == (6, 2, 2)
    assert [f.path for f in page.items] == sorted(golden)[2:4]


@requires_upload_api
@pytest.mark.req("CLI-03")
@pytest.mark.usefixtures("no_verify")
def test_find_all_iterates_every_page(hp: HelioPuddleClient, golden: dict[str, FileRecord]) -> None:
    assert [f.path for f in hp.find_all(order="path", page_size=4)] == sorted(golden)
    assert [f.path for f in hp.find_all(order="path", page_size=2)] == sorted(golden)
    assert [f.path for f in hp.find_all(path="y_*", page_size=1, order="path")] == [
        "y_v1.csv",
        "y_v2.csv",
    ]
    assert list(hp.find_all(path="nothing*")) == []


@requires_upload_api
@pytest.mark.req("CLI-03")
@pytest.mark.usefixtures("no_verify")
def test_list_versions_and_latest(hp: HelioPuddleClient, golden: dict[str, FileRecord]) -> None:
    by_path = hp.list_versions("x_v001.csv")
    assert by_path.series_id == golden["x_v001.csv"].series_id
    assert [f.path for f in by_path.items] == ["x_v002.csv", "x_v001.csv"]
    assert by_path.version_type == "number"
    assert [f.version_number for f in by_path.items] == [2, 1]
    by_id = hp.list_versions(golden["y_v1.csv"].series_id)
    assert [f.path for f in by_id.items] == ["y_v2.csv", "y_v1.csv"]

    assert hp.latest("x_v001.csv").path == "x_v002.csv"
    assert hp.latest("x_v002.csv").path == "x_v002.csv"
    assert hp.latest("y_v1.csv").path == "y_v2.csv"
    assert hp.latest("notes.txt").path == "notes.txt"
    assert hp.latest("sub/x_v003.csv").path == "sub/x_v003.csv"

    with pytest.raises(NotFoundError):
        hp.latest("missing_v001.csv")
    with pytest.raises(NotFoundError):
        hp.list_versions("missing_v001.csv")
    with pytest.raises(NotFoundError):
        hp.list_versions(99_999)


@requires_upload_api
@pytest.mark.req("CLI-03")
def test_remove_makes_previous_version_latest(
    hp: HelioPuddleClient, golden: dict[str, FileRecord], store: Path
) -> None:
    removed = hp.remove_file("x_v002.csv")
    assert removed.id == golden["x_v002.csv"].id
    assert removed.deleted_at is not None
    assert removed.state == FileState.DELETED
    assert not (store / "x_v002.csv").exists()

    assert hp.latest("x_v001.csv").path == "x_v001.csv"
    # The series is still found through the deleted path.
    assert hp.latest("x_v002.csv").path == "x_v001.csv"
    assert [f.path for f in hp.list_versions("x_v001.csv").items] == ["x_v001.csv"]
    assert [f.path for f in hp.list_versions("x_v002.csv", include_deleted=True).items] == [
        "x_v002.csv",
        "x_v001.csv",
    ]
    assert hp.get_file(removed.id).deleted_at is not None
    assert "x_v002.csv" not in [f.path for f in hp.find_all()]
    assert "x_v002.csv" in [f.path for f in hp.find_all(include_deleted=True)]

    with pytest.raises(NotFoundError):
        hp.remove_file("x_v002.csv")
    with pytest.raises(NotFoundError):
        hp.remove_file(removed.id)

    by_id = hp.remove_file(golden["x_v001.csv"].id)
    assert by_id.deleted_at is not None
    with pytest.raises(NotFoundError):
        hp.latest("x_v001.csv")


@requires_upload_api
@pytest.mark.req("CLI-03")
@pytest.mark.req("CLI-16")
def test_set_content_date(hp: HelioPuddleClient, tmp_path: Path) -> None:
    rec = hp.add_file(_local(tmp_path, "cd", b"cd"), "cd.dat")
    assert rec.content_date is None
    when = datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert hp.set_content_date("cd.dat", when).content_date == when
    assert hp.set_content_date(rec.id, None).content_date is None
    with pytest.raises(NotFoundError):
        hp.set_content_date(99_999, when)


@pytest.mark.usefixtures("no_verify")
def test_get_file_by_path_and_update_metadata(hp: HelioPuddleClient, tmp_path: Path) -> None:
    rec = hp.add_file(_local(tmp_path, "m", b"m"), "m.dat", metadata={"a": 1, "b": 2})
    assert hp.get_file_by_path("m.dat").id == rec.id
    with pytest.raises(NotFoundError):
        hp.get_file_by_path("nope.dat")

    merged = hp.update_metadata(rec.id, {"b": None, "c": 3})
    assert merged.metadata == {"a": 1, "c": 3}
    assert merged.updated_by == "alice"
    replaced = hp.update_metadata(rec.id, {"z": 0}, replace=True)
    assert replaced.metadata == {"z": 0}
    with pytest.raises(NotFoundError):
        hp.update_metadata(99_999, {"a": 1})


# --- CLI-06: open and download ----------------------------------------------


@requires_upload_api
@pytest.mark.req("CLI-06")
@pytest.mark.usefixtures("no_verify")
def test_open_reads_from_store(hp: HelioPuddleClient, golden: dict[str, FileRecord]) -> None:
    with hp.open("x_v001.csv") as f:
        assert f.read() == b"x1"
    with hp.open(golden["sub/x_v003.csv"].id) as f:
        assert f.read() == b"sx3"


@requires_upload_api
@pytest.mark.req("CLI-06")
@pytest.mark.usefixtures("no_verify")
def test_open_uses_first_available_copy(
    hp: HelioPuddleClient, tmp_path: Path, ctx: AppContext
) -> None:
    rec = hp.add_file(_local(tmp_path, "s", b"second"), "s.dat", storage_location="secondary")
    _set_state(ctx, rec.id, FileState.AVAILABLE)
    with hp.open("s.dat") as f:
        assert f.read() == b"second"


@requires_upload_api
@pytest.mark.req("CLI-06")
@pytest.mark.usefixtures("no_verify")
def test_open_and_download_without_available_copy(
    hp: HelioPuddleClient, tmp_path: Path, ctx: AppContext
) -> None:
    rec = hp.add_file(_local(tmp_path, "p", b"p"), "p.dat")
    _set_state(ctx, rec.id, FileState.UNKNOWN)
    with pytest.raises(FileNotFoundError, match="no available copy"):
        hp.open("p.dat")
    with pytest.raises(FileNotFoundError, match="no available copy"):
        hp.download(rec.id, tmp_path / "out.dat")
    assert not (tmp_path / "out.dat").exists()


@requires_upload_api
@pytest.mark.req("CLI-06", "STO-10")
def test_open_and_download_serve_the_original_during_an_overwrite(
    hp: HelioPuddleClient, tmp_path: Path, store: Path
) -> None:
    """While an overwrite is reserved (copy ``overwriting``) the original is still served."""
    rec = hp.add_file(_local(tmp_path, "o", b"original"), "o.dat")
    reservation = hp._request(  # pyright: ignore[reportPrivateUsage]
        "POST",
        f"/projects/{hp.project}/uploads",
        json={"path": "o.dat", "hash": _md5(b"replacement"), "size": 11, "overwrite": True},
    ).json()
    assert reservation["mode"] == "overwrite"
    (store / reservation["write_path"]).write_bytes(b"replacement")  # staged, not published

    assert hp.get_file(rec.id).state == FileState.OVERWRITING
    with hp.open("o.dat") as f:
        assert f.read() == b"original"
    assert hp.download(rec.id, tmp_path / "out.dat").read_bytes() == b"original"


@requires_upload_api
@pytest.mark.req("CLI-06")
@pytest.mark.usefixtures("no_verify")
def test_download_to_file_and_directory(
    hp: HelioPuddleClient, golden: dict[str, FileRecord], tmp_path: Path
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    target = hp.download("y_v2.csv", out / "renamed.csv")
    assert target == out / "renamed.csv"
    assert target.read_bytes() == b"y20"

    into_dir = hp.download(golden["sub/x_v003.csv"].id, out)
    assert into_dir == out / "x_v003.csv"
    assert into_dir.read_bytes() == b"sx3"

    as_str = hp.download("notes.txt", str(out / "deep" / "n.txt"))
    assert as_str.read_bytes() == b"n"
    assert sorted(p.name for p in out.iterdir()) == ["deep", "renamed.csv", "x_v003.csv"]


@requires_upload_api
@pytest.mark.req("CLI-06")
@pytest.mark.usefixtures("no_verify")
def test_download_tampered_content_raises_integrity_error(
    hp: HelioPuddleClient, golden: dict[str, FileRecord], store: Path, tmp_path: Path
) -> None:
    (store / "x_v001.csv").write_bytes(b"tampered")
    out = tmp_path / "out"
    out.mkdir()
    (out / "x_v001.csv").write_bytes(b"keep me")
    with pytest.raises(IntegrityError) as e:
        hp.download("x_v001.csv", out)
    err = e.value
    assert (err.expected, err.actual) == (_md5(b"x1"), _md5(b"tampered"))
    assert err.file.id == golden["x_v001.csv"].id
    # The partial download is removed and an existing file is left alone.
    assert sorted(p.name for p in out.iterdir()) == ["x_v001.csv"]
    assert (out / "x_v001.csv").read_bytes() == b"keep me"

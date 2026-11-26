"""CLI-14: the test harness's fake behaves like the real client against the API.

Every scenario in ``tests/fake_contract.py`` runs twice here: once with
``HelioPuddleClient`` against the in-process API, once with
``FakeHelioPuddleClient``. A change to the server that the fake does not
follow fails here. ``tests/integration/test_fake_contract_server.py`` runs the
same scenarios against a real ``heliopuddle server start``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from heliopuddle.client import (
    BusyError,
    HelioPuddleClient,
    HelioPuddleError,
    NotFoundError,
)
from heliopuddle.server.api import create_api
from heliopuddle.server.context import AppContext
from heliopuddle.server.schemas import FileState, HealthResponse
from heliopuddle.testing import Call, FakeHelioPuddleClient
from tests.fake_contract import SCENARIOS, Harness, Scenario
from tests.fake_contract_sync import SYNC_SCENARIOS


@pytest.fixture(params=["api", "fake"])
def h(request: pytest.FixtureRequest, tmp_path: Path, store: Path) -> Iterator[Harness]:
    secondary = tmp_path / "store2"
    if request.param == "fake":
        fake = FakeHelioPuddleClient(
            project="demo", store=store, storage_locations=["primary", "secondary"], user="alice"
        )
        yield Harness(fake, store, tmp_path, secondary)
        return
    ctx: AppContext = request.getfixturevalue("ctx")
    request.getfixturevalue("prefect_healthy")
    with (
        TestClient(create_api(ctx)) as http,
        HelioPuddleClient("http://testserver", "demo", user="alice", http_client=http) as c,
    ):
        yield Harness(c, store, tmp_path, secondary)


@pytest.mark.req("CLI-14")
@pytest.mark.parametrize("scenario", list(SCENARIOS.values()), ids=list(SCENARIOS))
def test_contract(h: Harness, scenario: Scenario) -> None:
    scenario(h)


@pytest.mark.req("CLI-14", "CLI-18")
@pytest.mark.parametrize("scenario", list(SYNC_SCENARIOS.values()), ids=list(SYNC_SCENARIOS))
def test_sync_contract(h: Harness, scenario: Scenario) -> None:
    scenario(h)


class TestFakeHelpers:
    @pytest.mark.req("CLI-14")
    def test_calls_are_recorded(self, heliopuddle_fake: FakeHelioPuddleClient) -> None:
        heliopuddle_fake.seed("s/a.txt", b"a")
        heliopuddle_fake.get_file_by_path("s/a.txt")
        assert heliopuddle_fake.calls == [Call("get_file_by_path", "s/a.txt")]
        assert heliopuddle_fake.methods_called() == ["get_file_by_path"]
        heliopuddle_fake.reset_calls()
        assert heliopuddle_fake.calls == []

    @pytest.mark.req("CLI-14")
    def test_seed_writes_the_store_and_sets_states(
        self, heliopuddle_fake: FakeHelioPuddleClient, heliopuddle_store: Path
    ) -> None:
        record = heliopuddle_fake.seed("s/a.txt", b"abc", metadata={"k": 1})
        assert (heliopuddle_store / "s/a.txt").read_bytes() == b"abc"
        assert record.hash == hashlib.md5(b"abc").hexdigest()
        stale = heliopuddle_fake.seed("s/b.txt", hash="0" * 32, size=1, state="unknown")
        assert stale.state == FileState.UNKNOWN
        assert heliopuddle_fake.live_paths() == ["s/a.txt", "s/b.txt"]
        assert heliopuddle_fake.record_at("s/a.txt") == record
        assert heliopuddle_fake.record_at("s/none.txt") is None
        assert heliopuddle_fake.calls == []
        with pytest.raises(ValueError):
            heliopuddle_fake.seed("s/a.txt", b"again")
        with pytest.raises(ValueError):
            heliopuddle_fake.seed("s/c.txt")

    @pytest.mark.req("CLI-14")
    def test_seed_with_a_version_and_its_series_kind(
        self, heliopuddle_fake: FakeHelioPuddleClient, heliopuddle_store: Path
    ) -> None:
        record = heliopuddle_fake.seed("s/a.txt", b"a", version_number=3)
        assert (record.version_number, record.version_text) == (3, None)
        heliopuddle_fake.remove_file(record.id)
        with pytest.raises(HelioPuddleError) as info:
            heliopuddle_fake.seed("s/a.txt", b"other", version_text="x")
        assert info.value.status_code == 422
        assert not (heliopuddle_store / "s/a.txt").exists()
        with pytest.raises(ValueError):
            heliopuddle_fake.seed("s/b.txt", b"b", version_number=1, version_text="1")

    @pytest.mark.req("CLI-14")
    @pytest.mark.parametrize(
        "override",
        [{"version_number": -1}, {"version_number": 2**63}, {"version_text": ""},
         {"version_text": "x" * 256}],
    )  # fmt: skip
    def test_out_of_range_versions_are_refused_like_the_client(
        self, heliopuddle_fake: FakeHelioPuddleClient, override: dict[str, Any]
    ) -> None:
        with pytest.raises(ValidationError):
            heliopuddle_fake.register_file("r.txt", "0" * 32, 1, **override)
        assert heliopuddle_fake.live_paths() == []

    @pytest.mark.req("CLI-14")
    def test_injected_busy_and_errors(self, heliopuddle_fake: FakeHelioPuddleClient) -> None:
        heliopuddle_fake.inject_busy("b.txt", times=2, retry_after=3)
        for _ in range(2):
            with pytest.raises(BusyError) as info:
                heliopuddle_fake.register_file("b.txt", "0" * 32, 1)
            assert info.value.retry_after == 3
        heliopuddle_fake.register_file("b.txt", "0" * 32, 1)

        heliopuddle_fake.inject_error("get_file_by_path", ConnectionError("down"))
        with pytest.raises(ConnectionError):
            heliopuddle_fake.get_file_by_path("b.txt")
        assert heliopuddle_fake.get_file_by_path("b.txt").path == "b.txt"

        heliopuddle_fake.inject_error("remove_file", RuntimeError("x"), path="b.txt")
        with pytest.raises(RuntimeError):
            heliopuddle_fake.remove_file("b.txt")

    @pytest.mark.req("CLI-14")
    def test_without_a_store_everything_is_trusted(self, tmp_path: Path) -> None:
        with FakeHelioPuddleClient() as fake:
            assert fake.register_file("a.txt", "0" * 32, 1).state == FileState.AVAILABLE
            source = tmp_path / "s"
            source.write_bytes(b"s")
            assert fake.add_file(source, "b.txt").state == FileState.AVAILABLE
            assert fake.health() == HealthResponse(status="ok", database="ok", prefect="ok")
        assert fake.closed
        with pytest.raises(ValueError):
            FakeHelioPuddleClient(storage_locations=[])

    @pytest.mark.req("CLI-14")
    def test_seed_checks_before_it_writes(
        self, heliopuddle_fake: FakeHelioPuddleClient, heliopuddle_store: Path
    ) -> None:
        heliopuddle_fake.seed("s/a.txt", b"first")
        with pytest.raises(ValueError):
            heliopuddle_fake.seed("s/a.txt", b"second")
        assert (heliopuddle_store / "s/a.txt").read_bytes() == b"first"

        outside = heliopuddle_store.parent / "outside.txt"
        for bad in ("../outside.txt", "s/../../outside.txt", "/abs.txt"):
            with pytest.raises(HelioPuddleError) as info:
                heliopuddle_fake.seed(bad, b"escape")
            assert info.value.status_code == 422
        assert not outside.exists()

        with pytest.raises(NotFoundError):
            heliopuddle_fake.seed("s/b.txt", b"b", storage_location="nowhere")
        assert not (heliopuddle_store / "s/b.txt").exists()
        assert heliopuddle_fake.live_paths() == ["s/a.txt"]

    @pytest.mark.req("CLI-14")
    def test_seed_writes_only_the_store_of_the_first_storage_location(self, tmp_path: Path) -> None:
        store = tmp_path / "store"
        fake = FakeHelioPuddleClient(store=store, storage_locations=["primary", "secondary"])
        record = fake.seed("s/a.txt", b"elsewhere", storage_location="secondary")
        assert [c.storage_location for c in record.storage_locations] == ["secondary"]
        assert not (store / "s/a.txt").exists()

    @pytest.mark.req("CLI-14")
    def test_seeded_content_counts_as_observed(
        self, heliopuddle_fake: FakeHelioPuddleClient, heliopuddle_store: Path
    ) -> None:
        heliopuddle_fake.seed("s/a.txt", b"seen")
        assert heliopuddle_fake.remove_file("s/a.txt").deleted_at is not None
        assert not (heliopuddle_store / "s/a.txt").exists()

        heliopuddle_fake.seed("s/b.txt", b"seen")
        (heliopuddle_store / "s/b.txt").write_bytes(b"changed afterwards")
        left = heliopuddle_fake.remove_file("s/b.txt")
        assert left.deleted_at is None
        assert left.state == FileState.UNKNOWN

    @pytest.mark.req("CLI-14")
    def test_a_failed_store_delete_leaves_the_copy_deleting(
        self,
        heliopuddle_fake: FakeHelioPuddleClient,
        heliopuddle_store: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        heliopuddle_fake.seed("s/a.txt", b"stuck")

        def refuse(self: Path, missing_ok: bool = False) -> None:
            raise PermissionError(str(self))

        monkeypatch.setattr(Path, "unlink", refuse)
        left = heliopuddle_fake.remove_file("s/a.txt")
        assert left.deleted_at is None
        assert left.state == FileState.DELETING
        assert (heliopuddle_store / "s/a.txt").exists()

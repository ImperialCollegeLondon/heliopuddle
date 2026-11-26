"""CLI-18 contract scenarios: ``sync_file`` and ``sync_removed`` on the fake and the real client.

Run like :mod:`tests.fake_contract` against ``FakeHelioPuddleClient`` and
against ``HelioPuddleClient`` with a real API (in process in
``tests/unit/test_testing_fake.py``, and real ``server start`` processes in
``tests/integration/test_fake_contract_server.py``).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from heliopuddle.client import SyncAction, SyncError
from heliopuddle.server.schemas import FileState
from tests.fake_contract import Harness, md5

SyncScenario = Callable[[Harness], None]
SYNC_SCENARIOS: dict[str, SyncScenario] = {}

MARCH_4 = datetime(2026, 3, 4, tzinfo=UTC)


def sync_scenario(fn: SyncScenario) -> SyncScenario:
    """Register ``fn`` as a sync contract scenario under its name."""
    SYNC_SCENARIOS[fn.__name__] = fn
    return fn


@sync_scenario
def sync_file_in_place_registers_then_is_unchanged(h: Harness) -> None:
    path = h.p("s/a.txt")
    h.put(path, b"one")

    added = h.client.sync_file(
        h.store / path, path, metadata={"k": 1}, in_place=True, content_date=MARCH_4
    )
    assert added.action == SyncAction.ADDED
    assert added.file is not None
    assert added.file.hash == md5(b"one")
    assert added.file.state == FileState.AVAILABLE
    assert added.file.metadata == {"k": 1}
    assert added.file.content_date == MARCH_4

    again = h.client.sync_file(
        h.store / path, path, metadata={"k": 1}, in_place=True, content_date=MARCH_4
    )
    assert again.action == SyncAction.UNCHANGED
    assert again.file is not None and again.file.id == added.file.id


@sync_scenario
def sync_file_uploads_then_overwrites_changed_content(h: Harness) -> None:
    path = h.p("s/b.txt")
    local = h.local("b.txt", b"first")
    added = h.client.sync_file(local, path)
    assert added.action == SyncAction.ADDED
    assert (h.store / path).read_bytes() == b"first"

    local.write_bytes(b"second version")
    updated = h.client.sync_file(local, path, metadata={"v": 2})
    assert updated.action == SyncAction.UPDATED
    assert updated.file is not None and added.file is not None
    assert updated.file.id == added.file.id
    assert updated.file.hash == md5(b"second version")
    assert updated.file.metadata == {"v": 2}
    assert (h.store / path).read_bytes() == b"second version"


@sync_scenario
def sync_file_with_the_same_content_replaces_metadata_and_sets_the_date(h: Harness) -> None:
    path = h.p("s/m.txt")
    local = h.local("m.txt", b"same")
    h.client.sync_file(local, path, metadata={"old": 0, "keep": 1})

    changed = h.client.sync_file(local, path, metadata={"keep": 1}, content_date=MARCH_4)
    assert changed.action == SyncAction.METADATA
    assert changed.file is not None
    assert changed.file.metadata == {"keep": 1}
    assert changed.file.content_date == MARCH_4
    # None leaves the metadata and date alone
    assert h.client.sync_file(local, path).action == SyncAction.UNCHANGED


@sync_scenario
def sync_file_in_place_accepts_content_heliopuddle_already_found(h: Harness) -> None:
    path = h.p("s/u.txt")
    h.put(path, b"new content")
    seen = h.client.register_file(path, md5(b"old"), 3)
    assert seen.state == FileState.UNKNOWN

    result = h.client.sync_file(h.store / path, path, in_place=True)
    assert result.action == SyncAction.UPDATED
    assert result.file is not None
    assert result.file.id == seen.id
    assert result.file.hash == md5(b"new content")
    assert result.file.state == FileState.AVAILABLE


@sync_scenario
def sync_file_in_place_replaces_content_changed_since_it_was_registered(h: Harness) -> None:
    path = h.p("s/c.txt")
    h.put(path, b"v1")
    first = h.client.sync_file(h.store / path, path, in_place=True)
    h.put(path, b"version 2")  # the writer changed it in place; hp has not looked yet

    result = h.client.sync_file(h.store / path, path, in_place=True)
    assert result.action == SyncAction.UPDATED
    assert result.file is not None and first.file is not None
    assert result.file.id == first.file.id
    assert result.file.hash == md5(b"version 2")
    assert result.file.state == FileState.AVAILABLE
    assert (h.store / path).read_bytes() == b"version 2"


@sync_scenario
def sync_file_in_place_without_the_content_in_the_store_fails(h: Harness) -> None:
    path = h.p("s/x.txt")
    h.put(path, b"something else")
    with pytest.raises(SyncError) as info:
        h.client.sync_file(h.local("x.txt", b"mine"), path, in_place=True)
    assert info.value.file.path == path
    assert info.value.file.state == FileState.UNKNOWN


@sync_scenario
def sync_file_dry_run_changes_nothing(h: Harness) -> None:
    path = h.p("s/d.txt")
    local = h.local("d.txt", b"dry")
    assert h.client.sync_file(local, path, dry_run=True).action == SyncAction.ADDED
    assert h.client.find(path=path).total == 0

    h.client.sync_file(local, path)
    assert h.client.sync_file(local, path, metadata={"a": 1}, dry_run=True).action == (
        SyncAction.METADATA
    )
    local.write_bytes(b"wet")
    assert h.client.sync_file(local, path, dry_run=True).action == SyncAction.UPDATED
    live = h.client.get_file_by_path(path)
    assert live.hash == md5(b"dry")
    assert live.metadata is None


@sync_scenario
def sync_file_uses_the_record_the_caller_already_has(h: Harness) -> None:
    path = h.p("s/k.txt")
    local = h.local("k.txt", b"known")
    assert h.client.sync_file(local, path, current=None).action == SyncAction.ADDED
    [record] = list(h.client.find_all(path=path))
    assert h.client.sync_file(local, path, current=record).action == SyncAction.UNCHANGED


@sync_scenario
def sync_file_needs_a_local_file(h: Harness) -> None:
    with pytest.raises(FileNotFoundError):
        h.client.sync_file(h.tmp / "nope", h.p("s/n.txt"))


@sync_scenario
def sync_removed_removes_once(h: Harness) -> None:
    path = h.p("s/r.txt")
    h.client.sync_file(h.local("r.txt", b"bye"), path)

    assert h.client.sync_removed(path, dry_run=True).action == SyncAction.REMOVED
    assert h.client.get_file_by_path(path).deleted_at is None

    removed = h.client.sync_removed(path)
    assert removed.action == SyncAction.REMOVED
    assert removed.file is not None and removed.file.deleted_at is not None
    assert not (h.store / path).exists()

    again = h.client.sync_removed(path)
    assert again.action == SyncAction.UNCHANGED
    assert again.file is None


@sync_scenario
def sync_removed_fails_when_changed_content_is_kept(h: Harness) -> None:
    path = h.p("s/k2.txt")
    h.client.sync_file(h.local("k2.txt", b"observed"), path)
    h.put(path, b"changed since hp observed it")

    with pytest.raises(SyncError) as info:
        h.client.sync_removed(path)
    assert info.value.file.deleted_at is None
    assert [c.state for c in info.value.file.storage_locations] == [FileState.UNKNOWN]
    assert (h.store / path).read_bytes() == b"changed since hp observed it"

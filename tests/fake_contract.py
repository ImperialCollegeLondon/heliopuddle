"""CLI-14 contract scenarios: the test fake must behave like the real client and API.

Each scenario takes a :class:`Harness` and runs unchanged against
``FakeHelioPuddleClient`` and against ``HelioPuddleClient`` talking to a real
API: in-process in ``tests/unit/test_testing_fake.py`` and a real
``heliopuddle server start`` (SQLite and Postgres) in
``tests/integration/test_fake_contract_server.py``. Every path is under a
unique prefix, so the scenarios also run against a server shared with other
tests.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from heliopuddle.client import (
    ConflictError,
    HelioPuddleClient,
    HelioPuddleError,
    NotFoundError,
)
from heliopuddle.server.schemas import FileState, HealthResponse
from heliopuddle.testing import FakeHelioPuddleClient


def md5(data: bytes) -> str:
    """Hex md5 of ``data``."""
    return hashlib.md5(data).hexdigest()


@dataclass
class Harness:
    """A client and the store of its project's first storage location.

    Attributes:
        client: the real client or the fake.
        store: root of the ``primary`` storage location.
        tmp: a scratch directory for local source files.
        secondary: root of a second storage location named ``secondary``, or
            None when the setup has only one.
        prefix: a unique first path segment for this scenario's files.
    """

    client: HelioPuddleClient | FakeHelioPuddleClient
    store: Path
    tmp: Path
    secondary: Path | None = None
    prefix: str = field(default_factory=lambda: f"t{uuid.uuid4().hex[:10]}")

    def p(self, path: str) -> str:
        """``path`` under this scenario's prefix."""
        return f"{self.prefix}/{path}"

    def put(self, path: str, data: bytes, root: Path | None = None) -> tuple[str, int]:
        """Write an object straight into a store (default ``primary``), as another tool would."""
        target = (root or self.store) / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return md5(data), len(data)

    def local(self, name: str, data: bytes) -> Path:
        """A local source file with ``data``."""
        path = self.tmp / "local" / self.prefix / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def needs_secondary(self) -> Path:
        """The ``secondary`` store; skips the scenario when the setup has none."""
        if self.secondary is None:
            pytest.skip("this setup has no secondary storage location")
        return self.secondary


Scenario = Callable[[Harness], None]
SCENARIOS: dict[str, Scenario] = {}


def scenario(fn: Scenario) -> Scenario:
    """Register ``fn`` as a contract scenario under its name."""
    SCENARIOS[fn.__name__] = fn
    return fn


@scenario
def health_is_a_health_response(h: Harness) -> None:
    health = h.client.health()
    assert isinstance(health, HealthResponse)
    assert health == HealthResponse(status="ok", database="ok", prefect="ok")


@scenario
def register_new_then_same_then_conflict(h: Harness) -> None:
    path = h.p("a/x.txt")
    digest, size = h.put(path, b"one")
    record = h.client.register_file(
        path, digest, size, metadata={"k": 1}, content_date=datetime(2026, 1, 2)
    )
    assert (record.path, record.name, record.hash, record.size) == (path, "x.txt", digest, size)
    assert record.state == FileState.AVAILABLE
    assert record.metadata == {"k": 1}
    assert record.is_latest
    assert record.created_by == "alice"
    assert record.content_date == datetime(2026, 1, 2, tzinfo=UTC)
    copy = record.storage_locations[0]
    assert (copy.storage_location, copy.hash, copy.size) == ("primary", digest, size)
    assert copy.last_seen_at is not None

    assert h.client.register_file(path, digest, size).id == record.id
    new_digest, new_size = h.put(path, b"two!")
    with pytest.raises(ConflictError) as info:
        h.client.register_file(path, new_digest, new_size)
    assert info.value.file.id == record.id


@scenario
def register_without_matching_object_is_unknown(h: Harness) -> None:
    path = h.p("a/none.txt")
    record = h.client.register_file(path, "0" * 32, 3)
    assert record.state == FileState.UNKNOWN
    copy = record.storage_locations[0]
    assert (copy.hash, copy.size, copy.last_seen_at) == (None, None, None)
    assert not record.is_latest
    with pytest.raises(NotFoundError):
        h.client.latest(path)

    h.put(path, b"wrong")
    mismatched = h.client.register_file(h.p("a/other.txt"), "0" * 32, 5)
    assert mismatched.state == FileState.UNKNOWN


@scenario
def register_same_hash_reverifies_a_copy_that_is_not_available(h: Harness) -> None:
    path = h.p("rv/a.txt")
    first = h.client.register_file(path, md5(b"late"), 4, metadata={"k": 1})
    assert first.state == FileState.UNKNOWN

    digest, size = h.put(path, b"late")
    again = h.client.register_file(path, digest, size, metadata={"k": 2})
    assert again.id == first.id
    assert again.state == FileState.AVAILABLE
    assert again.metadata == {"k": 1}
    assert again.is_latest
    copy = again.storage_locations[0]
    assert (copy.hash, copy.size) == (digest, size)
    assert copy.last_seen_at is not None

    # An available copy is not re-verified: a change behind hp's back goes unseen.
    h.put(path, b"changed behind its back")
    unchanged = h.client.register_file(path, digest, size)
    assert unchanged.state == FileState.AVAILABLE
    assert unchanged.storage_locations == again.storage_locations


@scenario
def register_same_file_in_another_storage_location_adds_a_copy(h: Harness) -> None:
    secondary = h.needs_secondary()
    path = h.p("two/a.txt")
    digest, size = h.put(path, b"both")
    first = h.client.register_file(path, digest, size, metadata={"k": 1})
    h.put(path, b"both", root=secondary)

    both = h.client.register_file(path, digest, size, storage_location="secondary")
    assert both.id == first.id
    assert both.metadata == {"k": 1}
    assert [(c.storage_location, c.state) for c in both.storage_locations] == [
        ("primary", FileState.AVAILABLE),
        ("secondary", FileState.AVAILABLE),
    ]
    found = h.client.find(path=h.p("**"), storage_location="secondary")
    assert [r.id for r in found.items] == [first.id]


@scenario
def invalid_paths_and_locations(h: Harness) -> None:
    for bad in ("/abs.txt", h.p("a/../b.txt"), h.p("a//b.txt"), h.p("x.hp-tmp")):
        with pytest.raises(HelioPuddleError) as info:
            h.client.register_file(bad, "0" * 32, 1)
        assert info.value.status_code == 422, bad
    with pytest.raises(NotFoundError):
        h.client.register_file(h.p("a.txt"), "0" * 32, 1, storage_location="nowhere")


@scenario
def add_file_uploads_and_is_idempotent(h: Harness) -> None:
    path = h.p("r/r.csv")
    source = h.local("r.csv", b"rows")
    record = h.client.add_file(source, path, metadata={"m": True})
    assert record.state == FileState.AVAILABLE
    assert record.metadata == {"m": True}
    assert (h.store / path).read_bytes() == b"rows"
    assert h.client.add_file(source, path).id == record.id


@scenario
def add_file_to_another_storage_location_adds_a_copy(h: Harness) -> None:
    h.needs_secondary()
    path = h.p("ac/a.txt")
    source = h.local("a", b"copy me")
    first = h.client.add_file(source, path, metadata={"k": 1})

    both = h.client.add_file(source, path, storage_location="secondary", metadata={"k": 2})
    assert both.id == first.id
    assert both.metadata == {"k": 1}
    assert [(c.storage_location, c.state) for c in both.storage_locations] == [
        ("primary", FileState.AVAILABLE),
        ("secondary", FileState.AVAILABLE),
    ]
    found = h.client.find(path=h.p("**"), storage_location="secondary")
    assert [r.id for r in found.items] == [first.id]


@scenario
def add_file_conflict_then_overwrite(h: Harness) -> None:
    path = h.p("o/a.txt")
    first = h.client.add_file(h.local("a", b"v1"), path)
    changed = h.local("b", b"version two")
    with pytest.raises(ConflictError):
        h.client.add_file(changed, path)
    replaced = h.client.add_file(changed, path, overwrite=True)
    assert replaced.id == first.id
    assert replaced.hash == md5(b"version two")
    assert (h.store / path).read_bytes() == b"version two"


@scenario
def add_file_missing_local_file(h: Harness) -> None:
    with pytest.raises(FileNotFoundError):
        h.client.add_file(h.tmp / "nope", h.p("n.txt"))


@scenario
def reads(h: Harness) -> None:
    path = h.p("q/a.txt")
    record = h.client.add_file(h.local("a", b"a"), path)
    assert h.client.get_file(record.id).path == path
    assert h.client.get_file_by_path(path).id == record.id
    assert h.client.latest(path).id == record.id
    with pytest.raises(NotFoundError):
        h.client.get_file_by_path(h.p("q/missing.txt"))
    with pytest.raises(NotFoundError):
        h.client.get_file(999_999_999)
    result = h.client.check(path, record.hash)
    assert (result.exists, result.hash_matches) == (True, True)
    assert h.client.check(path, "f" * 32).hash_matches is False
    assert h.client.check(h.p("q/b.txt"), record.hash).exists is False


@scenario
def remove_file(h: Harness) -> None:
    path = h.p("d/a.txt")
    record = h.client.add_file(h.local("a", b"a"), path)
    removed = h.client.remove_file(path)
    assert removed.deleted_at is not None
    assert not removed.is_latest
    assert [c.state for c in removed.storage_locations] == [FileState.DELETED]
    assert not (h.store / path).exists()
    with pytest.raises(NotFoundError):
        h.client.get_file_by_path(path)
    with pytest.raises(NotFoundError):
        h.client.remove_file(record.id)
    assert h.client.get_file(record.id).deleted_at is not None
    again = h.client.add_file(h.local("a", b"a"), path)
    assert again.id != record.id


@scenario
def remove_file_whose_object_is_already_gone(h: Harness) -> None:
    path = h.p("gone/a.txt")
    h.client.add_file(h.local("a", b"a"), path)
    (h.store / path).unlink()
    removed = h.client.remove_file(path)
    assert removed.deleted_at is not None
    assert [c.state for c in removed.storage_locations] == [FileState.DELETED]


@scenario
def remove_file_leaves_a_changed_object_as_unknown(h: Harness) -> None:
    path = h.p("ch/a.txt")
    record = h.client.add_file(h.local("a", b"observed"), path)
    h.put(path, b"changed since hp observed it")

    removed = h.client.remove_file(path)
    assert removed.id == record.id
    assert removed.deleted_at is None
    assert [c.state for c in removed.storage_locations] == [FileState.UNKNOWN]
    assert not removed.is_latest
    assert (h.store / path).read_bytes() == b"changed since hp observed it"
    assert h.client.get_file_by_path(path).id == record.id


@scenario
def remove_file_leaves_a_never_observed_object(h: Harness) -> None:
    path = h.p("nv/a.txt")
    record = h.client.register_file(path, md5(b"later"), 5)
    assert record.state == FileState.UNKNOWN
    h.put(path, b"later")  # written after the register: hp has never seen it

    removed = h.client.remove_file(record.id)
    assert removed.deleted_at is None
    assert [c.state for c in removed.storage_locations] == [FileState.UNKNOWN]
    assert (h.store / path).read_bytes() == b"later"


@scenario
def update_metadata_merge_and_replace(h: Harness) -> None:
    record = h.client.add_file(h.local("a", b"a"), h.p("m/a.txt"), metadata={"a": 1, "b": 2})
    merged = h.client.update_metadata(record.id, {"b": None, "c": 3})
    assert merged.metadata == {"a": 1, "c": 3}
    replaced = h.client.update_metadata(record.id, {"z": 1, "gone": None}, replace=True)
    assert replaced.metadata == {"z": 1}
    h.client.remove_file(record.id)
    with pytest.raises(NotFoundError):
        h.client.update_metadata(record.id, {"x": 1})


@scenario
def set_and_clear_content_date(h: Harness) -> None:
    record = h.client.add_file(h.local("cd", b"cd"), h.p("cd/a.txt"))
    assert record.content_date is None
    dated = h.client.set_content_date(h.p("cd/a.txt"), datetime(2026, 2, 3, 4, 5, 6))
    assert dated.content_date == datetime(2026, 2, 3, 4, 5, 6, tzinfo=UTC)
    assert h.client.set_content_date(record.id, None).content_date is None
    h.client.remove_file(record.id)
    with pytest.raises(NotFoundError):
        h.client.set_content_date(record.id, None)


@scenario
def accept_unknown_copy(h: Harness) -> None:
    path = h.p("u/a.txt")
    h.put(path, b"right")
    record = h.client.register_file(path, md5(b"wrong"), 5)
    assert record.state == FileState.UNKNOWN
    accepted = h.client.accept_file(path)
    assert accepted.state == FileState.AVAILABLE
    assert accepted.hash == md5(b"right")
    assert accepted.is_latest
    with pytest.raises(HelioPuddleError) as info:
        h.client.accept_file(path, storage_location="primary")
    assert info.value.status_code == 409


@scenario
def find_filters_order_and_paging(h: Harness) -> None:
    for name in ("f/b.csv", "f/a.csv", "f/sub/c.csv", "g/d.txt"):
        h.client.add_file(h.local(name.replace("/", "_"), name.encode()), h.p(name))
    h.client.remove_file(h.p("g/d.txt"))
    every = h.p("**")

    by_path = [r.path for r in h.client.find(path=every, order="path").items]
    assert by_path == [h.p("f/a.csv"), h.p("f/b.csv"), h.p("f/sub/c.csv")]
    assert [r.path for r in h.client.find(path=h.p("f/*.csv"), order="path").items] == [
        h.p("f/a.csv"),
        h.p("f/b.csv"),
    ]
    assert len(h.client.find(path=h.p("f/**")).items) == 3
    assert [r.path for r in h.client.find(path=every, deleted_only=True).items] == [h.p("g/d.txt")]
    assert h.client.find(path=every, include_deleted=True).total == 4
    page = h.client.find(path=every, order="path", limit=2, offset=1)
    assert ([r.path for r in page.items], page.total) == ([h.p("f/b.csv"), h.p("f/sub/c.csv")], 3)
    newest_first = [r.path for r in h.client.find(path=every).items]
    assert newest_first == [h.p("f/sub/c.csv"), h.p("f/a.csv"), h.p("f/b.csv")]
    assert h.client.find(path=every, latest_only=True).total == 3
    assert h.client.find(path=every, state="available").total == 3
    assert h.client.find(path=every, state=["missing", "unknown"]).total == 0
    assert h.client.find(path=every, ignored_from_all_rules=True).total == 0
    if h.secondary is not None:
        assert h.client.find(path=every, storage_location="secondary").total == 0
    assert [r.path for r in h.client.find_all(every, order="path", page_size=1)] == by_path
    # Positional filters as the real signature orders them: path, latest_only, state.
    positional = h.client.find_all(every, False, "available", page_size=2)
    assert [r.path for r in positional] == newest_first
    with pytest.raises(HelioPuddleError) as info:
        h.client.find(path=every, limit=0)
    assert info.value.status_code == 422


@scenario
def find_by_content_date(h: Harness) -> None:
    for day in (1, 2, 3):
        h.client.add_file(
            h.local(f"{day}", f"{day}".encode()),
            h.p(f"c/{day}.txt"),
            content_date=datetime(2026, 1, day),
        )
    h.client.add_file(h.local("none", b"none"), h.p("c/none.txt"))
    found = h.client.find(
        path=h.p("**"),
        content_date_from=datetime(2026, 1, 2, tzinfo=UTC),
        content_date_to=datetime(2026, 1, 3, tzinfo=UTC),
        order="path",
    )
    assert [r.path for r in found.items] == [h.p("c/2.txt"), h.p("c/3.txt")]


@scenario
def register_with_a_number_version_keeps_it(h: Harness) -> None:
    path = h.p("vn/a.txt")
    digest, size = h.put(path, b"numbered")
    record = h.client.register_file(path, digest, size, version_number=12)
    assert (record.version_number, record.version_text) == (12, None)
    assert record.is_latest

    assert h.client.register_file(path, digest, size).version_number == 12
    assert h.client.register_file(path, digest, size, version_number=12).id == record.id
    with pytest.raises(ConflictError) as info:
        h.client.register_file(path, digest, size, version_number=13)
    assert info.value.file.id == record.id
    assert info.value.file.version_number == 12
    assert h.client.get_file(record.id).version_number == 12


@scenario
def add_file_with_a_text_version_keeps_it_on_overwrite(h: Harness) -> None:
    path = h.p("vt/a.txt")
    first = h.client.add_file(h.local("a", b"one"), path, version_text="rc-1")
    assert (first.version_number, first.version_text) == (None, "rc-1")
    assert h.client.add_file(h.local("a", b"one"), path, version_text="rc-1").id == first.id

    replaced = h.client.add_file(h.local("b", b"two"), path, overwrite=True, version_text="rc-1")
    assert (replaced.id, replaced.version_text, replaced.hash) == (first.id, "rc-1", md5(b"two"))
    with pytest.raises(ConflictError) as info:
        h.client.add_file(h.local("c", b"three"), path, overwrite=True, version_text="rc-2")
    assert info.value.file.version_text == "rc-1"
    assert (h.store / path).read_bytes() == b"two"
    kept = h.client.add_file(h.local("d", b"four"), path, overwrite=True)
    assert kept.version_text == "rc-1"


@scenario
def without_an_override_and_no_rules_a_file_is_unversioned(h: Harness) -> None:
    record = h.client.add_file(h.local("u", b"u"), h.p("nv/a3.txt"))
    assert (record.version_number, record.version_text) == (None, None)
    with pytest.raises(ConflictError):
        h.client.add_file(h.local("u", b"u"), h.p("nv/a3.txt"), version_number=3)


@scenario
def both_version_overrides_are_refused_before_anything(h: Harness) -> None:
    path = h.p("vb/a.txt")
    digest, size = h.put(path, b"both")
    with pytest.raises(ValueError):
        h.client.register_file(path, digest, size, version_number=1, version_text="1")
    with pytest.raises(ValueError):
        h.client.add_file(h.local("b", b"both"), path, version_number=1, version_text="1")
    for number, text in ((-1, None), (2**63, None), (None, ""), (None, "x" * 256)):
        with pytest.raises(ValueError):
            h.client.register_file(path, digest, size, version_number=number, version_text=text)
    with pytest.raises(NotFoundError):
        h.client.get_file_by_path(path)


@scenario
def a_series_keeps_the_kind_of_its_first_version(h: Harness) -> None:
    path = h.p("vk/a.txt")
    record = h.client.add_file(h.local("a", b"a"), path, version_number=1)
    h.client.remove_file(record.id)
    with pytest.raises(HelioPuddleError) as info:
        h.client.add_file(h.local("a", b"a"), path, version_text="one")
    assert info.value.status_code == 422
    again = h.client.add_file(h.local("a", b"a"), path, version_number=2)
    assert again.version_number == 2

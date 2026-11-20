"""Adversarial uploads: reservations, expiry, takeover and complete/abort races."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from heliopuddle.server.context import AppContext
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services.upload import RepairResult, repair_expired
from heliopuddle.storage import StorageLocation
from tests.adversarial import (
    Clock,
    assert_invariants,
    client,
    clock,
    config_dir,  # noqa: F401 - fixture
    protocol,
    put,
    store,
)
from tests.lifecycle import copies, md5, patch_upload, store_bytes, temp_files, upload_attr
from tests.unit._adversarial_uploads_helpers import (
    NEW,
    NEWER,
    OLD,
    PATH,
    only_copy,
    reserve,
    upload,
)

_FIXTURES = (
    client,
    clock,
    protocol,
)  # referenced so ruff sees the imports used

# --- two reservations, expiry, takeover (STO-09) ------------------------------------


@pytest.mark.req("STO-09")
def test_two_reservations_on_one_path_exactly_one_owns_and_its_bytes_survive(
    ctx: AppContext, client: TestClient
) -> None:
    """B reserves while A owns the path: busy for different and equal content alike.

    A's bytes and record survive; B never receives a token or a write path.
    """
    loc = store(ctx)
    a = reserve(client, OLD).json()
    for data in (NEW, OLD):
        b = reserve(client, data)
        assert (b.status_code, b.json()["reason"]) == (409, "busy")
        assert "token" not in b.json()
    put(loc, a["write_path"], OLD)
    assert client.post(f"/uploads/{a['token']}/complete").status_code == 200
    assert store_bytes(loc, PATH) == OLD
    row = only_copy(ctx)
    assert (row.state, row.file_hash) == (FileState.AVAILABLE, md5(OLD))
    assert_invariants(ctx)


@pytest.mark.req("STO-09")
def test_expired_reservation_whose_bytes_match_is_kept_when_another_client_reserves(
    ctx: AppContext, client: TestClient, clock: Clock
) -> None:
    """A wrote the reserved bytes but never completed; its lease expires.

    B's reserve repairs A first. A's matching object must be recorded as
    ``available`` (not removed), B then conflicts, and A's late complete is answered
    ``200`` with the record: the repair published exactly what A reserved (owner decision;
    it used to be ``409``).
    """
    loc = store(ctx)
    a = reserve(client, OLD).json()
    put(loc, a["write_path"], OLD)
    clock.advance(timedelta(hours=2))

    b = reserve(client, NEW)
    assert b.status_code == 409 and "reason" not in b.json()  # conflict with A's record
    assert b.json()["file"]["hash"] == md5(OLD)
    assert store_bytes(loc, PATH) == OLD
    late = client.post(f"/uploads/{a['token']}/complete")
    assert late.status_code == 200 and late.json()["hash"] == md5(OLD)
    row = only_copy(ctx)
    assert (row.state, row.token) == (FileState.AVAILABLE, None)
    assert_invariants(ctx)


@pytest.mark.req("STO-09")
def test_abandoned_partial_new_upload_is_removed_by_the_next_reserve(
    ctx: AppContext, client: TestClient, clock: Clock
) -> None:
    """A crashes after writing part of the object; B's reserve repairs it and owns the path."""
    loc = store(ctx)
    a = reserve(client, OLD).json()
    put(loc, a["write_path"], OLD[:5])
    clock.advance(timedelta(hours=2))

    assert upload(client, loc, NEW)["hash"] == md5(NEW)
    assert store_bytes(loc, PATH) == NEW
    assert client.post(f"/uploads/{a['token']}/complete").status_code == 409
    assert_invariants(ctx)


@pytest.mark.req("STO-09")
@pytest.mark.parametrize("kind", ["new", "overwrite"])
def test_renew_complete_and_abort_after_takeover_change_nothing(
    ctx: AppContext, client: TestClient, clock: Clock, kind: str
) -> None:
    """A repair took the reservation over: A's renew, complete and abort are all refused.

    A's late write (a zombie) must not become part of the file's record either.
    """
    loc = store(ctx)
    if kind == "overwrite":
        upload(client, loc, OLD)
    a = reserve(client, NEW, overwrite=kind == "overwrite").json()
    clock.advance(timedelta(hours=2))
    assert repair_expired(only_copy(ctx).copy_id) in (RepairResult.REPAIRED, RepairResult.DROPPED)
    put(loc, a["write_path"], NEW)  # the zombie keeps writing

    for call in (
        lambda: client.post(f"/uploads/{a['token']}/renew"),
        lambda: client.post(f"/uploads/{a['token']}/complete"),
        lambda: client.delete(f"/uploads/{a['token']}"),
    ):
        assert call().status_code == 409
    if kind == "overwrite":
        row = only_copy(ctx)
        assert (row.state, row.file_hash) == (FileState.AVAILABLE, md5(OLD))
        assert store_bytes(loc, PATH) == OLD
    else:
        assert copies(ctx) == []
    assert_invariants(ctx, allow_temp=True)


# --- complete vs abort vs repair (STO-09, STO-10) -------------------------------------


@pytest.mark.req("STO-09")
@pytest.mark.parametrize("winner", ["complete", "abort"])
def test_complete_and_abort_racing_one_wins_the_other_is_refused(
    ctx: AppContext,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    winner: str,
) -> None:
    """The first call claims the upload; while it observes the object, the other arrives.

    The loser gets 409 and changes nothing; the winner's outcome is the final state.
    """
    loc = store(ctx)
    a = reserve(client, OLD).json()
    put(loc, a["write_path"], OLD)
    token = a["token"]
    seen: list[int] = []
    real_observe = upload_attr("observe")

    def observe(store_: StorageLocation, path: str) -> Any:
        if not seen:
            seen.append(1)
            other = (
                client.delete(f"/uploads/{token}")
                if winner == "complete"
                else client.post(f"/uploads/{token}/complete")
            )
            seen.append(other.status_code)
        return real_observe(store_, path)

    patch_upload(monkeypatch, "observe", observe)
    first = (
        client.post(f"/uploads/{token}/complete")
        if winner == "complete"
        else client.delete(f"/uploads/{token}")
    )

    assert seen[1] == 409
    assert first.status_code in (200, 204)
    rows = copies(ctx)
    if winner == "complete":
        assert [(r.state, r.token) for r in rows] == [(FileState.AVAILABLE, None)]
        assert store_bytes(loc, PATH) == OLD
    else:
        # The abort found the matching content and recorded it (never removed it).
        assert [(r.state, r.token) for r in rows] == [(FileState.AVAILABLE, None)]
    assert_invariants(ctx)


@pytest.mark.req("STO-09")
def test_repair_taking_over_a_stalled_new_complete_leaves_one_consistent_outcome(
    ctx: AppContext, client: TestClient, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A's complete has claimed the upload but stalls (its lease runs out); a repair settles it.

    When A resumes its guarded update is stale and dropped; the object is
    recorded exactly once and A is told the real state.
    """
    loc = store(ctx)
    a = reserve(client, OLD).json()
    put(loc, a["write_path"], OLD)
    real_observe = upload_attr("observe")
    fired: list[RepairResult] = []

    def observe(store_: StorageLocation, path: str) -> Any:
        result = real_observe(store_, path)
        if not fired:
            fired.append(RepairResult.INCOMPLETE)  # re-entrancy guard
            clock.advance(timedelta(hours=2))
            fired[0] = repair_expired(only_copy(ctx).copy_id)
        return result

    patch_upload(monkeypatch, "observe", observe)
    done = client.post(f"/uploads/{a['token']}/complete")

    assert fired == [RepairResult.REPAIRED]
    assert done.status_code == 200
    assert done.json()["state"] == "available"
    assert_invariants(ctx)


@pytest.mark.req("STO-10")
def test_repair_taking_over_a_stalled_publish_never_loses_either_version(
    ctx: AppContext,
    client: TestClient,
    clock: Clock,
    protocol: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A's publish stalls right after the staged object was moved to the final path.

    Its lease expires and a repair settles the copy (new content at the final
    path is recorded as published). When A resumes, the stale DB update is
    dropped; the final object is the new content, the DB agrees, no temporary
    file is left after a last repair.
    """
    loc = store(ctx)
    upload(client, loc, OLD)
    a = reserve(client, NEW, overwrite=True).json()
    put(loc, a["write_path"], NEW)
    fired: list[RepairResult] = []
    real_finish = upload_attr("_finish_overwrite")  # pyright: ignore[reportPrivateUsage]

    def finish(*args: Any, **kwargs: Any) -> None:
        if not fired:
            fired.append(RepairResult.INCOMPLETE)  # re-entrancy guard
            clock.advance(timedelta(hours=2))
            fired[0] = repair_expired(only_copy(ctx).copy_id)
        real_finish(*args, **kwargs)

    patch_upload(monkeypatch, "_finish_overwrite", finish)
    client.post(f"/uploads/{a['token']}/complete")

    assert fired == [RepairResult.REPAIRED]
    assert store_bytes(loc, PATH) == NEW
    row = only_copy(ctx)
    assert (row.state, row.token, row.file_hash) == (FileState.AVAILABLE, None, md5(NEW))
    assert_invariants(ctx, allow_temp=True)
    assert [t for t in temp_files(loc) if not t.endswith(".hp-old.hp-tmp")] == []


@pytest.mark.req("STO-10")
def test_stale_publish_cannot_replace_content_published_by_a_newer_overwrite(
    ctx: AppContext,
    client: TestClient,
    clock: Clock,
    protocol: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A's publish stalls before touching the store; its lease expires and a repair
    returns the copy to the original. B then overwrites with NEWER and publishes.
    When A resumes it must not change B's content: the final object stays NEWER.
    """
    loc = store(ctx)
    upload(client, loc, OLD)
    a = reserve(client, NEW, overwrite=True).json()
    put(loc, a["write_path"], NEW)
    real_mv = upload_attr("_mv")
    state = {"resumed": False}

    def mv(store_: StorageLocation, src: str, dst: str) -> None:
        if not state["resumed"]:
            state["resumed"] = True
            clock.advance(timedelta(hours=2))
            assert repair_expired(only_copy(ctx).copy_id) == RepairResult.REPAIRED
            b = upload(client, loc, NEWER, overwrite=True)
            assert b["hash"] == md5(NEWER)
        real_mv(store_, src, dst)

    patch_upload(monkeypatch, "_mv", mv)
    r = client.post(f"/uploads/{a['token']}/complete")

    assert r.status_code == 409  # A did not publish
    assert store_bytes(loc, PATH) == NEWER
    row = only_copy(ctx)
    assert (row.state, row.file_hash) == (FileState.AVAILABLE, md5(NEWER))
    assert_invariants(ctx)

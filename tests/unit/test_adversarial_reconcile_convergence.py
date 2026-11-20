"""Adversarial reconcile: convergence, failures and watermarks."""

from __future__ import annotations

import contextlib
import os
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from heliopuddle.server import models
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import RuleState
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services import delete as delete_mod
from heliopuddle.server.services import transfer
from heliopuddle.server.services.reconcile import reconcile
from tests.adversarial import assert_invariants
from tests.unit._adversarial_reconcile_helpers import (
    content,
    rl,
)
from tests.unit._index_helpers import put
from tests.unit.test_reconcile import (  # noqa: F401 - fixtures
    config_dir,
    dump,
    no_triggers,
    present,
    roots,
    snapshot_stores,
    state,
    use,
)

_FIXTURES = (roots,)  # referenced so ruff sees the imports used

pytestmark = pytest.mark.usefixtures("inline_prefect")


@pytest.mark.req("SVC-06")
def test_verify_true_second_run_touches_no_row(ctx: AppContext, roots: dict[str, Path]) -> None:
    """verify=True twice over a clean store must leave every DB row identical.

    Interleaving: none. Invariant 4 (SVC-06): ``verify_file`` rewrites
    ``updated_at`` (verify.py ``locked.updated_at = now``) and recomputes
    ``is_latest`` for every available copy it re-hashes even when nothing
    changed, so the second reconcile changes every copy row and moves the
    incremental change key of every file.
    """
    use(ctx)
    put(roots["primary"], "move/a.dat", b"alpha")
    put(roots["primary"], "move/b.dat", b"bravo")
    reconcile.fn("demo", verify=True)
    rows, stores = dump(ctx), snapshot_stores(roots)
    second = reconcile.fn("demo", verify=True)
    assert second.changed == 0
    assert snapshot_stores(roots) == stores
    assert dump(ctx) == rows


@pytest.mark.req("SVC-06")
def test_conflicting_copy_destination_converges(ctx: AppContext, roots: dict[str, Path]) -> None:
    """A destination holding different bytes is recorded once, then left alone.

    Invariant 4: run 1 (the index already records the differing destination as
    ``unknown``) settles; runs 2 and 3 report nothing and change no row or byte;
    the source is never touched.
    """
    use(ctx, rl("mirror", "copy", "primary", "secondary"))
    put(roots["primary"], "move/a.dat", b"alpha")
    put(roots["secondary"], "move/a.dat", b"DIFFERENT")
    reconcile.fn("demo")
    assert state(ctx, "secondary", "move/a.dat") == FileState.UNKNOWN
    rows, stores = dump(ctx), snapshot_stores(roots)
    for _ in range(2):
        again = reconcile.fn("demo")
        assert again.changed == 0, again
        assert dump(ctx) == rows
        assert snapshot_stores(roots) == stores
    assert content(roots["primary"], "move/a.dat") == b"alpha"
    assert_invariants(ctx)


@pytest.mark.req("SVC-06")
def test_conflicting_move_destination_keeps_source_and_converges(
    ctx: AppContext, roots: dict[str, Path]
) -> None:
    """A move onto a differing destination must keep the source (invariant 2) and converge."""
    use(ctx, rl("stage", "move", "primary", "secondary"))
    put(roots["primary"], "move/a.dat", b"alpha")
    put(roots["secondary"], "move/a.dat", b"DIFFERENT")
    reconcile.fn("demo")
    assert content(roots["primary"], "move/a.dat") == b"alpha"
    assert state(ctx, "secondary", "move/a.dat") == FileState.UNKNOWN
    rows, stores = dump(ctx), snapshot_stores(roots)
    again = reconcile.fn("demo")
    assert again.changed == 0, again
    assert dump(ctx) == rows and snapshot_stores(roots) == stores
    assert_invariants(ctx)


@pytest.mark.req("SVC-06")
def test_newer_destination_is_skipped_forever(ctx: AppContext, roots: dict[str, Path]) -> None:
    """update_only_if_newer with a newer, different destination: no change on any run."""
    use(ctx, rl("mirror", "copy", "primary", "secondary", update_only_if_newer=True))
    put(roots["primary"], "move/a.dat", b"alpha", age=timedelta(days=10))
    put(roots["secondary"], "move/a.dat", b"NEWER!", age=timedelta(days=1))
    reconcile.fn("demo")
    rows, stores = dump(ctx), snapshot_stores(roots)
    again = reconcile.fn("demo")
    assert again.changed == 0
    assert dump(ctx) == rows and snapshot_stores(roots) == stores
    assert_invariants(ctx)


@pytest.mark.req("SVC-06")
def test_externally_missing_and_unknown_copies_settle(
    ctx: AppContext, roots: dict[str, Path]
) -> None:
    """A copy gone from the store, and one corrupted behind the index, are reported once.

    Invariant 4 for the index phase: after the run that records ``missing`` /
    ``unknown``, a further reconcile must be a no-op.
    """
    use(ctx)
    gone = put(roots["primary"], "move/gone.dat", b"gone")
    bad = put(roots["primary"], "move/bad.dat", b"alpha")
    put(roots["primary"], "move/ok.dat", b"fine")
    reconcile.fn("demo")
    gone.unlink()
    before = bad.stat()
    bad.write_bytes(b"ALPHA")
    os.utime(bad, ns=(before.st_atime_ns, before.st_mtime_ns))
    first = reconcile.fn("demo", verify=True)
    assert first.changed > 0
    rows, stores = dump(ctx), snapshot_stores(roots)
    for verify in (False, False):
        again = reconcile.fn("demo", verify=verify)
        assert again.changed == 0, (verify, again)
    assert dump(ctx) == rows and snapshot_stores(roots) == stores
    assert_invariants(ctx)


@pytest.mark.req("SVC-06")
def test_destination_raced_in_as_newer_file_is_counted(
    ctx: AppContext, roots: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A newer destination appearing between ``transfer_file``'s two destination looks.

    Interleaving: ``transfer_file`` observes the destination absent (decision
    ``write``); before ``_write_destination`` reserves the copy, an upload lands
    a newer different file there. ``_write_destination`` then records the
    destination copy (``finish_new`` -> ``unknown``) yet returns ``skipped``, so
    reconcile reports changed == 0 although a DB row changed (invariant 4/change
    counting: ``transfer.py`` ``_skip`` outcome for ``newer`` after ``finish_new``).
    """
    use(ctx, rl("mirror", "copy", "primary", "secondary", update_only_if_newer=True))
    put(roots["primary"], "move/a.dat", b"alpha", age=timedelta(days=10))
    reconcile.fn("demo", rules=[])  # index only
    real = transfer.upload.object_present

    def racing(*args: Any, **kwargs: Any) -> Any:
        put(roots["secondary"], "move/a.dat", b"NEWER!", age=timedelta(days=1))
        return real(*args, **kwargs)

    monkeypatch.setattr(transfer.upload, "object_present", racing)
    before = dump(ctx)
    summary = reconcile.fn("demo", rules=["mirror"])
    monkeypatch.setattr(transfer.upload, "object_present", real)
    after = dump(ctx)
    db_changed = before != after
    if db_changed:
        assert summary.changed > 0, "DB rows changed but reconcile reported no change"
    assert_invariants(ctx)


@pytest.mark.req("SVC-28")
def test_single_failure_with_max_failures_one_stops_immediately(
    ctx: AppContext, roots: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """max_failures=1: the first failed file stops everything; later files and rules untouched."""
    use(ctx, rl("r1", "move", "primary", "secondary"), rl("r2", "copy", "secondary", "tertiary"))
    ctx.projects["demo"].reconcile.max_failures = 1
    for name in "abc":
        put(roots["primary"], f"move/{name}.dat", name.encode())
    real = transfer.transfer_file
    attempts: list[int] = []

    def boom(file_id: int, *a: Any, **k: Any) -> Any:
        attempts.append(file_id)
        if len(attempts) == 1:
            raise OSError("locked")
        return real(file_id, *a, **k)

    monkeypatch.setattr(transfer, "transfer_file", boom)
    with pytest.raises(RuntimeError, match="stopped after 1 failed"):
        reconcile.fn("demo")
    assert len(attempts) == 1
    assert present(roots["tertiary"]) == set()
    assert len(present(roots["primary"])) == 3
    assert_invariants(ctx)


@pytest.mark.req("SVC-28")
def test_failure_in_last_rule_of_a_later_pass_still_fails_and_keeps_sources(
    ctx: AppContext, roots: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure in the last rule of pass 2 fails the run; no third pass; sources intact.

    The delete rule is listed first so it only acts in pass 2, after the move rule
    fed it in pass 1; the purges fail.
    """
    use(ctx, rl("purge", "delete", "secondary"), rl("stage", "move", "primary", "secondary"))
    for name in "ab":
        put(roots["primary"], f"move/{name}.dat", name.encode())
    real = delete_mod.delete_object
    calls = {"n": 0}

    def selective(store: Any, owned: Any, **k: Any) -> Any:
        calls["n"] += 1
        if calls["n"] <= 2:  # the two move source deletes in pass 1
            return real(store, owned, **k)
        raise OSError("rm denied")

    monkeypatch.setattr(delete_mod, "delete_object", selective)
    with pytest.raises(RuntimeError, match="in pass 2"):
        reconcile.fn("demo")
    assert calls["n"] == 4  # two moves + two failed purges, and no pass 3
    assert_invariants(ctx)


@pytest.mark.req("SVC-28")
def test_failed_move_never_deletes_source_and_next_run_recovers(
    ctx: AppContext, roots: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A move whose destination write is torn keeps its source; a clean rerun completes it."""
    use(ctx, rl("stage", "move", "primary", "secondary"))
    put(roots["primary"], "move/a.dat", b"alpha")
    put(roots["primary"], "move/b.dat", b"bravo")
    real = transfer._copy_bytes  # pyright: ignore[reportPrivateUsage]

    def broken(src: Any, dst: Any, path: str, write_path: str, check: Any) -> None:
        real(src, dst, path, write_path, check)
        if path == "move/a.dat":
            dst.fs.pipe(dst.full_path(write_path), b"torn")

    monkeypatch.setattr(transfer, "_copy_bytes", broken)
    with pytest.raises(RuntimeError, match="1 file"):
        reconcile.fn("demo")
    assert content(roots["primary"], "move/a.dat") == b"alpha"
    assert present(roots["primary"]) == {"move/a.dat"}
    assert_invariants(ctx)
    monkeypatch.setattr(transfer, "_copy_bytes", real)
    reconcile.fn("demo")
    assert present(roots["primary"]) == set()
    assert content(roots["secondary"], "move/a.dat") == b"alpha"
    assert_invariants(ctx)


@pytest.mark.req("SVC-28")
def test_failed_file_leaves_rule_watermark_untouched(
    ctx: AppContext, roots: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 7: a reconcile with a failed file writes no rule_state row at all."""
    use(ctx, rl("stage", "copy", "primary", "secondary"))
    for name in "abc":
        put(roots["primary"], f"move/{name}.dat", name.encode())
    real = transfer.transfer_file

    def boom(file_id: int, *a: Any, **k: Any) -> Any:
        if file_id == 2:
            raise OSError("locked")
        return real(file_id, *a, **k)

    monkeypatch.setattr(transfer, "transfer_file", boom)
    with pytest.raises(RuntimeError):
        reconcile.fn("demo")
    with ctx.sessionmaker() as session:
        assert list(session.scalars(select(RuleState))) == []


@pytest.mark.req("SVC-26")
def test_inactive_destination_is_not_written(ctx: AppContext, roots: dict[str, Path]) -> None:
    """Invariant 6: a copy rule to an inactive storage location writes nothing there."""
    use(ctx, rl("mirror", "copy", "primary", "tertiary"))
    put(roots["primary"], "move/a.dat", b"alpha")
    with ctx.write_session() as session:
        for loc in models.list_storage_locations(session, "demo"):
            if loc.name == "tertiary":
                loc.active = False
    with contextlib.suppress(Exception):
        reconcile.fn("demo")
    assert present(roots["tertiary"]) == set()
    assert present(roots["primary"]) == {"move/a.dat"}
    assert_invariants(ctx)


@pytest.mark.req("SVC-26")
def test_rule_disabled_mid_pass_stops_after_its_batch(
    ctx: AppContext, roots: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rule disabled in the DB while its pass runs processes no further batch."""
    use(ctx, rl("stage", "move", "primary", "secondary", batch={"max_files": 1}))
    for name in "abc":
        put(roots["primary"], f"move/{name}.dat", name.encode())
    real = transfer.transfer_file

    def disabling(*a: Any, **k: Any) -> Any:
        result = real(*a, **k)
        with ctx.write_session() as session:
            session.merge(RuleState(project_id=1, rule_name="stage", enabled=False))
        return result

    monkeypatch.setattr(transfer, "transfer_file", disabling)
    reconcile.fn("demo")
    assert len(present(roots["secondary"])) == 1
    assert len(present(roots["primary"])) == 2
    assert_invariants(ctx)


@pytest.mark.req("SVC-26")
def test_ping_pong_moves_fail_without_losing_the_file(
    ctx: AppContext, roots: dict[str, Path]
) -> None:
    """Moves A->B and B->A never converge; the run fails and the content stays in one place."""
    use(
        ctx, rl("there", "move", "primary", "secondary"), rl("back", "move", "secondary", "primary")
    )
    put(roots["primary"], "move/a.dat", b"alpha")
    with pytest.raises(RuntimeError, match="fixed point"):
        reconcile.fn("demo")
    holders = [n for n in ("primary", "secondary") if content(roots[n], "move/a.dat") == b"alpha"]
    assert len(holders) == 1
    assert_invariants(ctx)

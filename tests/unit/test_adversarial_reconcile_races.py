"""Adversarial reconcile: changes mid-pass, phases and ignores."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from heliopuddle.server import models
from heliopuddle.server.context import AppContext
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


@pytest.mark.req("SVC-26")
def test_source_changed_after_destination_verified_is_not_deleted(
    ctx: AppContext, roots: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariants 2/3: the source is rewritten after read-back but before its delete.

    The move must refuse (stale source) and the newer source bytes survive.
    """
    use(ctx, rl("stage", "move", "primary", "secondary"))
    put(roots["primary"], "move/a.dat", b"alpha")
    real = transfer.transfers.finish_transfer

    def racing(*a: Any, **k: Any) -> Any:
        result = real(*a, **k)
        put(roots["primary"], "move/a.dat", b"NEWER CONTENT")
        return result

    monkeypatch.setattr(transfer.transfers, "finish_transfer", racing)
    with pytest.raises(RuntimeError):
        reconcile.fn("demo")
    assert content(roots["primary"], "move/a.dat") == b"NEWER CONTENT"
    monkeypatch.setattr(transfer.transfers, "finish_transfer", real)
    reconcile.fn("demo", rules=[])  # re-index the external change
    assert_invariants(ctx)


@pytest.mark.req("SVC-26")
def test_destination_corrupted_after_readback_keeps_source(
    ctx: AppContext, roots: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 2: the destination rots between read-back and the source delete."""
    use(ctx, rl("stage", "move", "primary", "secondary"))
    put(roots["primary"], "move/a.dat", b"alpha")
    real = transfer.transfers.finish_transfer

    def racing(*a: Any, **k: Any) -> Any:
        result = real(*a, **k)
        put(roots["secondary"], "move/a.dat", b"ROTTED")
        return result

    monkeypatch.setattr(transfer.transfers, "finish_transfer", racing)
    with pytest.raises(RuntimeError):
        reconcile.fn("demo")
    assert content(roots["primary"], "move/a.dat") == b"alpha"
    monkeypatch.setattr(transfer.transfers, "finish_transfer", real)
    reconcile.fn("demo", rules=[])  # re-index the external change
    assert_invariants(ctx)


@pytest.mark.req("SVC-26")
def test_move_onto_identical_unrecorded_destination_deletes_only_after_verify(
    ctx: AppContext, roots: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``unchanged`` path of a move: destination identical and recorded.

    The destination rots after the (trusting) index of a later run; the move
    re-hashes it, so the source must survive and nothing is deleted unverified.
    """
    use(ctx, rl("stage", "move", "primary", "secondary"))
    put(roots["primary"], "move/a.dat", b"alpha")
    dest = put(roots["secondary"], "move/a.dat", b"alpha")
    reconcile.fn("demo", rules=[])  # both indexed available
    before = dest.stat()
    dest.write_bytes(b"ALPHA")  # same size, same mtime: the index trusts it
    os.utime(dest, ns=(before.st_atime_ns, before.st_mtime_ns))
    with contextlib.suppress(RuntimeError):
        reconcile.fn("demo")
    assert content(roots["primary"], "move/a.dat") == b"alpha"
    assert_invariants(ctx)


@pytest.mark.req("SVC-26")
def test_index_failure_stops_before_any_rule(
    ctx: AppContext, roots: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-per-file error in the index phase aborts before rules touch the stores."""
    from heliopuddle.server.services import reconcile as reconcile_mod

    use(ctx, rl("stage", "move", "primary", "secondary"))
    put(roots["primary"], "move/a.dat", b"alpha")

    def broken(project: str, name: str) -> Any:
        if name == "secondary":
            raise OSError("listing failed")
        yield from ()

    monkeypatch.setattr(reconcile_mod, "index_full_chain", broken)
    with pytest.raises(RuntimeError, match="listing failed") as raised:
        reconcile.fn("demo")
    assert isinstance(raised.value.__cause__, OSError)
    assert present(roots["primary"]) == {"move/a.dat"}
    assert present(roots["secondary"]) == set()


@pytest.mark.req("SVC-26")
def test_two_rules_to_one_destination_converge(ctx: AppContext, roots: dict[str, Path]) -> None:
    """Two rules copying the same path into one destination converge and then change nothing."""
    use(ctx, rl("a", "copy", "primary", "tertiary"), rl("b", "copy", "secondary", "tertiary"))
    put(roots["primary"], "move/a.dat", b"alpha")
    put(roots["secondary"], "move/a.dat", b"alpha")
    reconcile.fn("demo")
    rows, stores = dump(ctx), snapshot_stores(roots)
    again = reconcile.fn("demo")
    assert again.changed == 0
    assert dump(ctx) == rows and snapshot_stores(roots) == stores
    assert_invariants(ctx)


@pytest.mark.req("SVC-28")
def test_index_failure_names_phase_and_storage_location(
    ctx: AppContext, roots: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed index batch must fail the run naming the phase and storage location.

    docs/design/rules.md "Failures" and SVC-28. ``reconcile._index_changes`` lets the
    raw store error escape (reconcile.py ``_index_changes``/``reconcile``), so the
    message names neither "index" nor the storage location.
    """
    from heliopuddle.server.services import reconcile as reconcile_mod

    use(ctx)
    put(roots["primary"], "move/a.dat", b"alpha")

    def broken(project: str, name: str) -> Any:
        if name == "secondary":
            raise OSError("listing failed")
        yield from ()

    monkeypatch.setattr(reconcile_mod, "index_full_chain", broken)
    with pytest.raises(Exception, match="listing failed") as raised:
        reconcile.fn("demo")
    text = str(raised.value).lower()
    assert "index" in text and "secondary" in text, text


@pytest.mark.req("SVC-28")
def test_verify_failure_names_phase_and_storage_location(
    ctx: AppContext, roots: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed verify batch must fail the run naming the phase and storage location (see above)."""
    from heliopuddle.server.services import reconcile as reconcile_mod

    use(ctx)
    put(roots["primary"], "move/a.dat", b"alpha")

    def broken(copy_id: int) -> Any:
        raise OSError("read failed")

    monkeypatch.setattr(reconcile_mod, "verify_file", broken)
    with pytest.raises(Exception, match="read failed") as raised:
        reconcile.fn("demo", verify=True)
    text = str(raised.value).lower()
    assert "verify" in text and "primary" in text, text


@pytest.mark.req("SVC-28")
def test_non_file_error_in_a_later_rule_does_not_lose_recorded_failures(
    ctx: AppContext, roots: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failures already recorded in a pass are listed even if a later rule errors outright.

    Interleaving: rule r1 fails one file (recorded, pass continues); r2 then raises a
    non-per-file error (its source storage location went inactive: ``_resolve``
    raises NotFoundError). ``_rule_pass`` lets that escape, so the run fails without
    listing r1's failed file (SVC-28: "fails listing every failed file").
    """
    use(ctx, rl("r1", "copy", "primary", "secondary"), rl("r2", "copy", "tertiary", "primary"))
    put(roots["primary"], "move/a.dat", b"alpha")
    with ctx.write_session() as session:
        for loc in models.list_storage_locations(session, "demo"):
            if loc.name == "tertiary":
                loc.active = False

    def boom(file_id: int, *a: Any, **k: Any) -> Any:
        raise OSError("locked-file")

    monkeypatch.setattr(transfer, "transfer_file", boom)
    with pytest.raises(Exception) as raised:
        reconcile.fn("demo")
    assert "locked-file" in str(raised.value), str(raised.value)


@pytest.mark.req("SVC-26")
def test_ignore_flag_set_mid_pass_is_honoured(
    ctx: AppContext, roots: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file flagged ignored while the pass runs is not moved when its turn comes."""
    from heliopuddle.server.database.orm import File

    use(ctx, rl("stage", "move", "primary", "secondary"))
    for name in "abc":
        put(roots["primary"], f"move/{name}.dat", name.encode())
    real = transfer.transfer_file
    done: list[int] = []

    def flagging(file_id: int, *a: Any, **k: Any) -> Any:
        done.append(file_id)
        if len(done) == 1:
            with ctx.write_session() as session:
                for f in session.scalars(select(File).where(File.id != file_id)):
                    f.ignored_from_all_rules = True
        return real(file_id, *a, **k)

    monkeypatch.setattr(transfer, "transfer_file", flagging)
    reconcile.fn("demo")
    assert len(done) == 1
    assert len(present(roots["primary"])) == 2
    assert_invariants(ctx)


@pytest.mark.req("SVC-26")
def test_storage_location_url_change_mid_pass_blocks_the_move(
    ctx: AppContext, roots: dict[str, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The destination's configured URL changes mid-pass: later moves must not write or delete."""
    use(ctx, rl("stage", "move", "primary", "secondary"))
    for name in "abc":
        put(roots["primary"], f"move/{name}.dat", name.encode())
    real = transfer.transfer_file
    calls = {"n": 0}

    def changing(file_id: int, *a: Any, **k: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:
            cfg = ctx.storage_location_config("demo", "secondary")
            assert cfg is not None
            cfg.url = f"file://{tmp_path / 'elsewhere'}"
        return real(file_id, *a, **k)

    monkeypatch.setattr(transfer, "transfer_file", changing)
    with pytest.raises(RuntimeError):
        reconcile.fn("demo")
    assert len(present(roots["primary"])) >= 2  # only the first file may have moved
    assert not (tmp_path / "elsewhere").exists() or not present(tmp_path / "elsewhere")

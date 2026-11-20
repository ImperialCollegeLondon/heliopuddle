"""Reconcile: index, verify and rule passes to a fixed point (SVC-06, SVC-26, SVC-28)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from heliopuddle.config.models import RuleConfig
from heliopuddle.server import models
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import File, FileStorageLocation
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services import transfer
from heliopuddle.server.services.reconcile import reconcile
from tests.adversarial import assert_invariants
from tests.unit._index_helpers import put

pytestmark = pytest.mark.usefixtures("inline_prefect")

CONFIG = """\
project: demo
index:
  pause_between_runs: 0s
storage_locations:
{locations}
"""


@pytest.fixture(autouse=True)
def no_triggers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Change triggers need a Prefect server; they are not under test here."""
    monkeypatch.setattr(transfer, "_notify_file_changes", lambda *a, **k: None)


@pytest.fixture
def roots(tmp_path: Path) -> dict[str, Path]:
    return {name: tmp_path / name for name in ("primary", "secondary", "tertiary")}


@pytest.fixture
def config_dir(tmp_path: Path, roots: dict[str, Path]) -> Path:
    path = tmp_path / "config"
    path.mkdir()
    locations = "\n".join(
        f"  - name: {name}\n    url: file://{root}\n    storage_options:\n      auto_mkdir: true"
        for name, root in roots.items()
    )
    (path / "demo.yaml").write_text(CONFIG.format(locations=locations))
    return path


def rule(name: str, action: str, source: str, destination: str | None = None, **kw: Any) -> Any:
    return RuleConfig.model_validate(
        {
            "name": name,
            "action": action,
            "source": source,
            "destination": destination,
            "match": {"paths": [kw.pop("paths", "move/**")]},
            "options": {"pause_between_runs": "0s", "check_for_space": False},
            **kw,
        }
    )


def use(ctx: AppContext, *rules: RuleConfig) -> None:
    ctx.projects["demo"].rules = list(rules)


def present(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def dump(ctx: AppContext) -> list[tuple[Any, ...]]:
    """Every file and copy row, without the pure observation time ``last_seen_at``."""
    with ctx.sessionmaker() as session:
        rows: list[tuple[Any, ...]] = []
        for table in (File, FileStorageLocation):
            for row in session.scalars(select(table).order_by(table.id)):
                rows.append(
                    tuple(
                        (c.name, getattr(row, c.name))
                        for c in table.__table__.columns
                        if c.name != "last_seen_at"
                    )
                )
        return rows


def snapshot_stores(roots: dict[str, Path]) -> dict[str, dict[str, bytes]]:
    return {
        name: {p: (root / p).read_bytes() for p in present(root)} for name, root in roots.items()
    }


def state(ctx: AppContext, location: str, path: str) -> FileState:
    with ctx.sessionmaker() as session:
        for copy in session.scalars(select(FileStorageLocation)):
            if copy.storage_location.name == location and copy.file.path == path:
                return copy.state
    raise AssertionError((location, path))


@pytest.mark.req("SVC-06")
def test_reconcile_reaches_a_fixed_point_and_a_second_run_changes_nothing(
    ctx: AppContext, roots: dict[str, Path]
) -> None:
    use(
        ctx,
        rule("purge", "delete", "secondary"),
        rule("stage", "move", "primary", "secondary"),
    )
    put(roots["primary"], "move/a.dat", b"alpha")
    put(roots["primary"], "move/b.dat", b"bravo")
    put(roots["primary"], "keep/c.dat", b"charlie")
    first = reconcile.fn("demo")
    assert [p.changed > 0 for p in first.passes] == [True, True, False]
    assert first.passes[0].changes == {"purge": 0, "stage": 2}
    assert first.passes[1].changes == {"purge": 2, "stage": 0}
    assert first.index_changes["primary"] == 3
    assert present(roots["primary"]) == {"keep/c.dat"}
    assert present(roots["secondary"]) == set()
    assert_invariants(ctx)

    rows, stores = dump(ctx), snapshot_stores(roots)
    second = reconcile.fn("demo")
    assert second.changed == 0
    assert dump(ctx) == rows
    assert snapshot_stores(roots) == stores
    assert_invariants(ctx)


@pytest.mark.req("SVC-06")
def test_copy_rule_reaches_a_fixed_point_and_does_not_recopy(
    ctx: AppContext, roots: dict[str, Path]
) -> None:
    use(ctx, rule("mirror", "copy", "primary", "secondary"))
    put(roots["primary"], "move/a.dat", b"alpha")
    first = reconcile.fn("demo")
    assert first.passes[0].changes == {"mirror": 1}
    assert present(roots["secondary"]) == {"move/a.dat"}
    rows = dump(ctx)
    again = reconcile.fn("demo")
    assert again.changed == 0
    assert dump(ctx) == rows


@pytest.mark.req("SVC-26")
def test_a_pair_that_undoes_itself_fails_naming_both_rules(
    ctx: AppContext, roots: dict[str, Path]
) -> None:
    use(ctx, rule("mirror", "copy", "primary", "secondary"), rule("purge", "delete", "secondary"))
    put(roots["primary"], "move/a.dat", b"alpha")
    with pytest.raises(RuntimeError, match="fixed point in 3 passes") as raised:
        reconcile.fn("demo")
    assert "mirror" in str(raised.value) and "purge" in str(raised.value)
    assert present(roots["primary"]) == {"move/a.dat"}
    assert_invariants(ctx)


@pytest.mark.req("SVC-26")
def test_max_rule_passes_is_configurable(ctx: AppContext, roots: dict[str, Path]) -> None:
    use(ctx, rule("purge", "delete", "secondary"), rule("stage", "move", "primary", "secondary"))
    ctx.projects["demo"].reconcile.max_rule_passes = 2
    put(roots["primary"], "move/a.dat", b"alpha")
    with pytest.raises(RuntimeError, match="fixed point in 2 passes") as raised:
        reconcile.fn("demo")
    assert "purge" in str(raised.value)


@pytest.mark.req("SVC-26")
def test_verify_finds_corruption_that_the_index_trusts(
    ctx: AppContext, roots: dict[str, Path]
) -> None:
    use(ctx)
    target = put(roots["primary"], "move/a.dat", b"alpha")
    assert reconcile.fn("demo").index_changes["primary"] == 1
    before = target.stat()
    target.write_bytes(b"ALPHA")  # same size
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    plain = reconcile.fn("demo")
    assert plain.changed == 0 and plain.verify_changes == {}
    assert state(ctx, "primary", "move/a.dat") == FileState.AVAILABLE
    checked = reconcile.fn("demo", verify=True)
    assert checked.verify_changes == {"primary": 1, "secondary": 0, "tertiary": 0}
    assert state(ctx, "primary", "move/a.dat") == FileState.UNKNOWN
    assert_invariants(ctx)


@pytest.mark.req("SVC-26")
def test_a_rule_subset_leaves_other_rules_files_alone(
    ctx: AppContext, roots: dict[str, Path]
) -> None:
    use(
        ctx,
        rule("ra", "move", "primary", "secondary", paths="a/**"),
        rule("rb", "move", "primary", "secondary", paths="b/**"),
    )
    put(roots["primary"], "a/1.dat", b"one")
    put(roots["primary"], "b/2.dat", b"two")
    summary = reconcile.fn("demo", rules=["ra"])
    assert summary.rules == ["ra"]
    assert present(roots["secondary"]) == {"a/1.dat"}
    assert present(roots["primary"]) == {"b/2.dat"}
    # the index still covers every storage location
    assert summary.index_changes["primary"] == 2


@pytest.mark.req("SVC-26")
def test_a_disabled_rule_is_skipped_and_listed(ctx: AppContext, roots: dict[str, Path]) -> None:
    use(
        ctx,
        rule("ra", "move", "primary", "secondary", paths="a/**"),
        rule("rb", "move", "primary", "secondary", paths="b/**", enabled=False),
    )
    put(roots["primary"], "a/1.dat", b"one")
    put(roots["primary"], "b/2.dat", b"two")
    summary = reconcile.fn("demo")
    assert (summary.rules, summary.skipped_rules) == (["ra"], ["rb"])
    assert present(roots["primary"]) == {"b/2.dat"}
    named = reconcile.fn("demo", rules=["rb"])
    assert (named.rules, named.skipped_rules, named.passes) == ([], ["rb"], [])


@pytest.mark.req("SVC-26")
def test_unknown_rule_and_project_are_rejected(ctx: AppContext) -> None:
    use(ctx, rule("ra", "copy", "primary", "secondary"))
    with pytest.raises(ValueError, match="nope"):
        reconcile.fn("demo", rules=["ra", "nope"])
    with pytest.raises(models.NotFoundError):
        reconcile.fn("missing")


@pytest.mark.req("SVC-26")
def test_inactive_storage_locations_are_not_indexed(
    ctx: AppContext, roots: dict[str, Path]
) -> None:
    use(ctx)
    put(roots["tertiary"], "x.dat", b"x")
    with ctx.write_session() as session:
        for loc in models.list_storage_locations(session, "demo"):
            if loc.name == "tertiary":
                loc.active = False
    summary = reconcile.fn("demo")
    assert set(summary.index_changes) == {"primary", "secondary"}


def fail_on(monkeypatch: pytest.MonkeyPatch, *names: str) -> list[str]:
    """Make ``transfer_file`` raise for files named in ``names``; return every attempt."""
    real = transfer.transfer_file
    attempts: list[str] = []

    def patched(file_id: int, source: str, destination: str, **kwargs: Any) -> Any:
        with models_session() as session:
            path = session.get_one(File, file_id).path
        attempts.append(path)
        if path in names:
            raise OSError(f"boom {path}")
        return real(file_id, source, destination, **kwargs)

    def models_session() -> Any:
        from heliopuddle.server.context import get_context

        return get_context().sessionmaker()

    monkeypatch.setattr(transfer, "transfer_file", patched)
    return attempts


@pytest.mark.req("SVC-28")
def test_a_failed_file_does_not_stop_the_pass_or_start_another(
    ctx: AppContext, roots: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    use(
        ctx, rule("r1", "move", "primary", "secondary"), rule("r2", "copy", "secondary", "tertiary")
    )
    for name in ("a", "b", "c"):
        put(roots["primary"], f"move/{name}.dat", name.encode())
    attempts = fail_on(monkeypatch, "move/b.dat")
    with pytest.raises(RuntimeError, match="1 file") as raised:
        reconcile.fn("demo")
    assert "r1: move/b.dat" in str(raised.value) and "boom" in str(raised.value)
    assert present(roots["primary"]) == {"move/b.dat"}
    assert present(roots["secondary"]) == {"move/a.dat", "move/c.dat"}
    assert present(roots["tertiary"]) == {"move/a.dat", "move/c.dat"}  # the next rule ran
    assert attempts.count("move/b.dat") == 1  # no second pass
    assert_invariants(ctx)


@pytest.mark.req("SVC-28")
def test_reaching_max_failures_fails_at_once(
    ctx: AppContext, roots: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    use(
        ctx, rule("r1", "move", "primary", "secondary"), rule("r2", "copy", "secondary", "tertiary")
    )
    ctx.projects["demo"].reconcile.max_failures = 2
    for name in ("a", "b", "c", "d"):
        put(roots["primary"], f"move/{name}.dat", name.encode())
    attempts = fail_on(monkeypatch, "move/a.dat", "move/b.dat")
    with pytest.raises(RuntimeError, match="stopped after 2 failed") as raised:
        reconcile.fn("demo")
    assert "move/a.dat" in str(raised.value) and "move/b.dat" in str(raised.value)
    assert attempts == ["move/a.dat", "move/b.dat"]
    assert present(roots["primary"]) == {f"move/{n}.dat" for n in "abcd"}
    assert present(roots["tertiary"]) == set()
    assert_invariants(ctx)


@pytest.mark.req("SVC-26")
def test_reconcile_without_rules_only_indexes(ctx: AppContext, roots: dict[str, Path]) -> None:
    use(ctx)
    put(roots["primary"], "a.dat", b"a")
    summary = reconcile.fn("demo")
    assert summary.passes == [] and summary.changed == 1


# --- review fixes: per-batch rule slot and enablement, chain finished elsewhere ----------


def batched(name: str, action: str, source: str, destination: str | None = None) -> Any:
    """A rule whose batches hold one file, so a chain has several batches."""
    return RuleConfig.model_validate(
        {
            "name": name,
            "action": action,
            "source": source,
            "destination": destination,
            "match": {"paths": ["move/**"]},
            "options": {
                "pause_between_runs": "1s",
                "check_for_space": False,
                "batch": {"max_files": 1},
            },
        }
    )


@pytest.mark.req("SVC-26", "SVC-25")
def test_a_rule_disabled_during_a_pause_runs_no_further_batch(
    ctx: AppContext, roots: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from heliopuddle.server.services import rules as rules_service

    use(ctx, batched("stage", "move", "primary", "secondary"))
    for name in ("a", "b", "c"):
        put(roots["primary"], f"move/{name}.dat", name.encode())
    disabled = {"now": False}
    real_enabled = rules_service._enabled
    monkeypatch.setattr(
        rules_service, "_enabled", lambda c, r: not disabled["now"] and real_enabled(c, r)
    )
    monkeypatch.setattr(rules_service, "sleep", lambda _s: disabled.update(now=True))

    summary = reconcile.fn("demo")

    assert len(present(roots["secondary"])) == 1  # only the batch before the pause
    assert len(present(roots["primary"])) == 2
    assert summary.passes[0].changes == {"stage": 1}
    assert_invariants(ctx)


@pytest.mark.req("SVC-26")
def test_the_rule_slot_is_released_while_pausing_between_batches(
    ctx: AppContext, roots: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import contextmanager

    from heliopuddle.server.services import rules as rules_service

    use(ctx, batched("stage", "copy", "primary", "secondary"))
    for name in ("a", "b"):
        put(roots["primary"], f"move/{name}.dat", name.encode())
    held = {"slot": False}
    seen: list[bool] = []

    @contextmanager
    def slot(*_args: Any, **_kwargs: Any) -> Any:
        held["slot"] = True
        try:
            yield
        finally:
            held["slot"] = False

    monkeypatch.setattr(rules_service, "hp_concurrency", slot)
    monkeypatch.setattr(rules_service, "sleep", lambda _s: seen.append(held["slot"]))

    reconcile.fn("demo")

    assert seen and not any(seen)  # every pause ran with the slot released
    assert present(roots["secondary"]) == {"move/a.dat", "move/b.dat"}


@pytest.mark.req("SVC-26")
def test_a_full_index_chain_finished_by_another_run_is_not_an_error(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    from heliopuddle.server.services import index as index_service

    calls: list[Any] = []

    def batch(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        return index_service.IndexSummary(
            project="demo", storage_location="primary", mode="full", remaining=True
        )

    monkeypatch.setattr(index_service, "_run", batch)
    monkeypatch.setattr(index_service.index_db, "index_checkpoint", lambda *a, **k: None)

    summaries = index_service.index_full_chain("demo", "primary")

    assert len(summaries) == 1 and len(calls) == 1

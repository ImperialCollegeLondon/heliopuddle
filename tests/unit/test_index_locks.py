"""Index runs against Prefect global concurrency limits (SVC-03, SVC-08).

These tests need a Prefect server: the per-storage-location lock is a global
concurrency limit. The other index tests run the flows with ``flow.fn``.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest

from heliopuddle.server.context import AppContext
from heliopuddle.server.models import index as index_db
from heliopuddle.server.services.concurrency import (
    ConcurrencyBusyError,
    ensure_concurrency_limit,
    hp_concurrency,
)
from heliopuddle.server.services.index import index_full, index_incremental
from heliopuddle.server.services.index_names import storage_location_lock

pytestmark = pytest.mark.usefixtures("prefect_harness")


def set_index(ctx: AppContext, **values: Any) -> None:
    cfg = ctx.projects["demo"].index
    for key, value in values.items():
        setattr(cfg, key, value)


@pytest.mark.req("SVC-08")
def test_incremental_skips_when_storage_location_is_busy(ctx: AppContext) -> None:
    lock = storage_location_lock("demo", "primary")
    ensure_concurrency_limit(lock)
    with hp_concurrency(lock, timeout_seconds=10):
        summary = index_incremental("demo", "primary")
    assert summary.skipped_run
    assert summary.listed == 0
    with ctx.sessionmaker() as session:
        assert index_db.get_index_progress(session, "demo", "primary") is None
    # Free again: it runs.
    assert not index_incremental("demo", "primary").skipped_run


@pytest.mark.req("SVC-03")
def test_full_waits_for_lock_up_to_lock_timeout(ctx: AppContext) -> None:
    set_index(ctx, lock_timeout=timedelta(seconds=1))
    lock = storage_location_lock("demo", "secondary")
    ensure_concurrency_limit(lock)
    with hp_concurrency(lock, timeout_seconds=10), pytest.raises(TimeoutError):
        index_full("demo", "secondary")


@pytest.mark.req("SVC-08")
def test_hp_concurrency_without_waiting() -> None:
    name = f"test:{uuid.uuid4().hex[:8]}"
    ensure_concurrency_limit(name)
    ensure_concurrency_limit(name)  # existing: left alone
    with (
        hp_concurrency(name, wait=False),
        pytest.raises(ConcurrencyBusyError),
        hp_concurrency(name, wait=False),
    ):
        pass  # pragma: no cover
    with hp_concurrency(name, wait=False):
        pass


@pytest.mark.req("SVC-08")
def test_hp_concurrency_without_waiting_reraises_other_errors() -> None:
    from prefect.concurrency.asyncio import ConcurrencySlotAcquisitionError

    with (
        pytest.raises(ConcurrencySlotAcquisitionError),
        hp_concurrency(f"absent:{uuid.uuid4().hex[:8]}", strict=True, wait=False),
    ):
        pass  # pragma: no cover

"""Global concurrency limits through Prefect (see docs/design/architecture.md).

hp has no locks or queues of its own beyond DB row locks: work that must not
overlap takes a slot from a Prefect global concurrency limit with
:func:`hp_concurrency`. Limit names are ``storage-location:<project>:<name>``
and ``rule:<project>:<name>``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager

import httpx
from prefect.client.orchestration import get_client
from prefect.client.schemas.actions import GlobalConcurrencyLimitCreate
from prefect.concurrency.asyncio import ConcurrencySlotAcquisitionError
from prefect.concurrency.sync import concurrency
from prefect.exceptions import ObjectAlreadyExists, ObjectNotFound


class ConcurrencyBusyError(TimeoutError):
    """A limit had no free slot and the caller asked not to wait (``wait=False``)."""


def storage_location_limit(project: str, name: str) -> str:
    """Return the concurrency limit name for a storage location."""
    return f"storage-location:{project}:{name}"


def rule_limit(project: str, name: str) -> str:
    """Return the concurrency limit name for a rule."""
    return f"rule:{project}:{name}"


def ensure_concurrency_limit(name: str, limit: int = 1) -> None:
    """Create the Prefect global concurrency limit ``name`` if it does not exist.

    An existing limit is left as it is (an operator may have changed it). A
    creation race with another process is resolved by reading the limit again.

    Args:
        name: limit name, for example from :func:`storage_location_limit`.
        limit: slots of a newly created limit.
    """
    with get_client(sync_client=True) as client:
        try:
            client.read_global_concurrency_limit_by_name(name)
            return
        except ObjectNotFound:
            pass
        try:
            client.create_global_concurrency_limit(
                GlobalConcurrencyLimitCreate(name=name, limit=limit)
            )
        except ObjectAlreadyExists:
            # Lost a creation race (SVC-18): the winner's limit must now be readable.
            client.read_global_concurrency_limit_by_name(name)


@contextmanager
def hp_concurrency(
    names: str | list[str],
    occupy: int = 1,
    timeout_seconds: float | None = None,
    strict: bool = False,
    wait: bool = True,
) -> Iterator[None]:
    """Hold slots of Prefect global concurrency limits for the ``with`` block.

    Wraps :func:`prefect.concurrency.sync.concurrency`. A limit that does not
    exist is ignored unless ``strict``.

    Args:
        names: limit name(s), for example from :func:`storage_location_limit`.
        occupy: slots to take from each limit.
        timeout_seconds: give up waiting after this long (None waits forever).
        strict: raise if a limit does not exist.
        wait: when False, try once and raise :class:`ConcurrencyBusyError` at
            once if no slot is free.

    Raises:
        TimeoutError: if the slots are not acquired within ``timeout_seconds``.
        ConcurrencyBusyError: if ``wait`` is False and a limit is full.
    """
    with ExitStack() as stack:
        try:
            stack.enter_context(
                concurrency(
                    names,
                    occupy=occupy,
                    timeout_seconds=timeout_seconds,
                    max_retries=None if wait else 0,
                    strict=strict,
                )
            )
        except ConcurrencySlotAcquisitionError as e:
            cause = e.__cause__
            if (
                not wait
                and isinstance(cause, httpx.HTTPStatusError)
                and cause.response.status_code == 423
            ):
                raise ConcurrencyBusyError(f"concurrency limit busy: {names}") from e
            raise
        yield

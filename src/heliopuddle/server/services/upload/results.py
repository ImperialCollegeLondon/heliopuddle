"""Result types, errors and outcome tables shared by the upload modules."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from heliopuddle.server.schemas import FileState

REPAIR_USER = "heliopuddle:index"
"""``updated_by`` recorded when a repair settles a copy (the indexer's system user)."""


class RepairResult(StrEnum):
    """What :func:`repair_expired` did.

    ``not_owned_or_live``: nothing to repair (no such copy, no expired
    operation, or a live one owns it). ``repaired``: the copy was settled
    (``available``, ``unknown``, ``missing`` or ``deleted``). ``dropped``: an
    abandoned new-file reservation was removed. ``incomplete``: a store or
    config error stopped the repair; it is retried by the next one.
    """

    NOT_OWNED_OR_LIVE = "not_owned_or_live"
    REPAIRED = "repaired"
    DROPPED = "dropped"
    INCOMPLETE = "incomplete"


class _Settled(StrEnum):
    PUBLISHED = "published"
    ORIGINAL = "original"
    UNKNOWN = "unknown"
    DROPPED = "dropped"
    INCOMPLETE = "incomplete"
    STALE = "stale"
    UNOBSERVED = "unobserved"


_TO_REPAIR = {
    _Settled.PUBLISHED: RepairResult.REPAIRED,
    _Settled.ORIGINAL: RepairResult.REPAIRED,
    _Settled.UNKNOWN: RepairResult.REPAIRED,
    _Settled.DROPPED: RepairResult.DROPPED,
    _Settled.INCOMPLETE: RepairResult.INCOMPLETE,
    _Settled.STALE: RepairResult.NOT_OWNED_OR_LIVE,
    _Settled.UNOBSERVED: RepairResult.INCOMPLETE,
}


_AFTER = {
    _Settled.ORIGINAL: "the original is kept",
    _Settled.UNKNOWN: "the copy is unknown",
    _Settled.DROPPED: "the upload was dropped",
    _Settled.INCOMPLETE: "the copy will be repaired",
    _Settled.STALE: "the copy will be repaired",
}


class PublishFailed(Exception):
    """An overwrite did not publish; the copy kept (or returned to) the original.

    Attributes:
        file_id: the file.
    """

    def __init__(self, file_id: int, message: str) -> None:
        super().__init__(message)
        self.file_id = file_id


class ObjectUnobservable(Exception):
    """Complete could not read the uploaded object (a transient store error).

    The reservation is left in place (still owned by the client's token,
    generation unchanged) so the client can retry complete; otherwise the
    next repair settles it after its deadline (``503``).

    Attributes:
        file_id: the file.
    """

    def __init__(self, file_id: int, path: str) -> None:
        super().__init__(
            f"could not read {path!r} to verify it; the upload is kept, retry complete"
        )
        self.file_id = file_id


@dataclass(frozen=True)
class CompleteResult:
    """Result of :func:`complete_upload`: the file and its copy's state."""

    file_id: int
    state: FileState

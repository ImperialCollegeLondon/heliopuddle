"""The DB half of the upload protocol (STO-09, STO-10). See docs/design/operations.md.

These functions never touch a store. The store half (writing, publishing,
repair) is :mod:`heliopuddle.server.services.upload`; it reads a
:class:`CopySnapshot`, does its I/O outside any transaction, then applies the
result here through a guarded transition
(:func:`heliopuddle.server.models.transitions.lock_copy`), which drops it if
the copy changed meanwhile.

A reservation that creates its copy row leaves ``generation`` at 0 until it
completes; a reservation on an existing copy bumps it. So ``generation == 0``
on a token-holding ``pending`` copy means "created by this reservation", and
only then may an abandoned upload's object and rows be removed.

Split by responsibility: :mod:`.types` (reservations, errors), :mod:`.reserve`,
:mod:`.lifecycle` (a client's token: renew, claim) and :mod:`.settle` (finish,
drop, abandon, find expired).
"""

from heliopuddle.server.models.files import ConflictError
from heliopuddle.server.models.snapshots import CopySnapshot, snapshot
from heliopuddle.server.models.transitions import NeedsRepair
from heliopuddle.server.models.uploads.lifecycle import (
    claim,
    content_expectations,
    project_name_of_upload,
    published_by_repair,
    renew,
)
from heliopuddle.server.models.uploads.reserve import reserve
from heliopuddle.server.models.uploads.settle import (
    abandon,
    drop_new,
    find_expired,
    finish_new,
    finish_overwrite,
    release_overwrite,
    take_over_expired,
    unclaim,
)
from heliopuddle.server.models.uploads.types import (
    NotOverwritable,
    Reservation,
    TokenNotCurrent,
    new_token,
)

__all__ = [
    "ConflictError",
    "CopySnapshot",
    "NeedsRepair",
    "NotOverwritable",
    "Reservation",
    "TokenNotCurrent",
    "abandon",
    "claim",
    "content_expectations",
    "drop_new",
    "find_expired",
    "finish_new",
    "finish_overwrite",
    "new_token",
    "project_name_of_upload",
    "published_by_repair",
    "release_overwrite",
    "renew",
    "reserve",
    "snapshot",
    "take_over_expired",
    "unclaim",
]

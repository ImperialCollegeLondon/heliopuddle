"""The store half of the upload protocol: complete, publish, abort and repair (STO-09, STO-10).

See docs/design/operations.md ("Add a file", "Overwrite", "Expiry and
repair"). The DB half is :mod:`heliopuddle.server.models.uploads`. Every
function here does its store I/O outside any transaction and applies the
result through a guarded transition, so a step acting on a copy that was
taken over is dropped.

The invariant kept at every step of an overwrite: the final path holds the
complete original or the complete verified new content (on a backend whose
move does not replace, a failed publish *and* a failed restore can leave the
original only in the backup, which repair restores), and the DB hash of an
``available`` copy equals the store's.

Modules: :mod:`.results` (result types), :mod:`.objects` (store helpers),
:mod:`.settle_new` and :mod:`.settle_overwrite` (settling an owned copy),
:mod:`.complete` (complete, publish, abort), :mod:`.repair` (expired
operations) and :mod:`.content` (API content writes).
"""

from heliopuddle.server.services.upload.complete import (
    abort_upload,
    complete_upload,
    publish_overwrite,
)
from heliopuddle.server.services.upload.content import ContentWriteFailed, write_upload_content
from heliopuddle.server.services.upload.objects import (
    object_present,
    temp_files,
)
from heliopuddle.server.services.upload.repair import repair_expired
from heliopuddle.server.services.upload.results import (
    CompleteResult,
    ObjectUnobservable,
    PublishFailed,
    RepairResult,
)

__all__ = [
    "CompleteResult",
    "ContentWriteFailed",
    "ObjectUnobservable",
    "PublishFailed",
    "RepairResult",
    "abort_upload",
    "complete_upload",
    "object_present",
    "publish_overwrite",
    "repair_expired",
    "temp_files",
    "write_upload_content",
]

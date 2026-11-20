# Core operations

Every operation that changes a file's content in a storage location — upload,
overwrite, delete, verify, index, accept, and copy/move in M4 — spans several
phases: decisions in the DB, I/O on the store, and a final DB update. Row
locks only protect single DB transactions, so hp adds one small contract on
top: **an operation owns the copy it changes, and every state change carries
the revision it expects.** This document is that contract; states and columns
are in [data model](data-model.md).

## Ownership

A copy (`file_storage_location`) is **owned** by an operation while its
`op_token` is set and `op_deadline` has not passed. Ownership reuses the copy
state:

| State | With a live token | Without a token |
|---|---|---|
| `pending` | an upload or transfer is reserved or being written (not latest-eligible) | known content awaiting verification (latest-eligible) |
| `overwriting` | old content still valid and served (latest-eligible); new content staged | — |
| `deleting` | a delete in progress | an earlier delete failed; repaired by the index |

Any other operation on an owned (project, path, storage location) gets
`409 busy` with `Retry-After` (seconds until the owner's deadline, at least
1) and the body `{detail, reason: "busy", copy_id, deadline}`; the index
skips owned copies. Tokens are unique per copy: a delete of a file with two
copies holds two tokens. A copy holding an *expired* token, or `deleting`
without one, is repaired first ([expiry and repair](#expiry-and-repair)) by
whoever next reserves, registers or deletes it.

An operation changes only the copy it owns. When a publish, an accept or an
index changes the file's hash, the file's *other* copies hold the old content
and become `unknown`, except those owned by an operation (any token, live or
expired): they are left alone and settle themselves against the new file hash
when their owner completes, aborts or is repaired (a publish records its own
verified hash; anything else that no longer matches becomes `unknown`).

A token-holding `pending` copy is never latest-eligible, whether or not its
token has expired, so `is_latest` never depends on the clock.

## Guarded transitions

All copy state changes go through one helper (`server/models/transitions.py`,
`lock_copy`). Under the series row lock it re-reads the copy and checks the
operation's token (another live token → `CopyBusy`, `409 busy`), the expected
`generation`, that the file is still live, the expected hash and the allowed
current states; if anything differs it raises `StaleOperation` and the change
is dropped and logged. Content-changing transitions increment `generation`
(upload completion, publish, delete, accept, a register that resets a copy,
and a reservation on an existing copy). Verify, delete, accept, index and
upload completion all carry the generation they started from.

A reservation that creates its copy row leaves `generation` at 0 until it
completes, so a token-holding `pending` copy at generation 0 is one hp
created for that upload; only such a copy's object (and rows) are removed
when the upload is abandoned.

## Add a file (upload)

1. The client hashes the local file (md5, streamed).
2. **Reserve:** `POST /projects/{p}/uploads {path, hash, size,
   storage_location?, overwrite?, metadata?, content_date?}`. Under the series
   lock the server answers `200` with `{mode, token, write_path, deadline,
   file}`:
   - same hash and the copy is `available` → `unchanged`: no token, nothing
     is written;
   - same hash and the copy is absent, `pending`, `unknown`, `missing` or
     `deleted` → `new` on that copy (`pending` with a token, generation
     bumped); `write_path` is the final path;
   - different hash, no `overwrite` → `409` conflict `{detail, file}` with the
     existing record (the client raises `ConflictError` without touching the
     store);
   - owned by another operation → `409 busy` with `Retry-After` (the client
     raises `BusyError` with `retry_after`, without touching the store);
   - new path → `new`: a file row (series and version from the path rules,
     `metadata`, `content_date`; not latest) and a copy `pending` with a
     token; `write_path` is the final path;
   - **never write directly over an existing object:** a `new` reservation
     (new path, or the copy is absent, `pending`, `unknown`, `missing` or
     `deleted`) for which an object already exists at the final path (an
     `unknown` copy being uploaded again, or an object hp has never observed)
     gets a unique staging `write_path` `<path>.<token>.hp-tmp` instead, and
     `pending_staged` is set on the copy. It is published at complete with the
     same procedure as an [overwrite](#overwrite) (verify the staged hash, move
     into place with a backup restored on failure, re-verify). The existing
     object is never truncated by a failed write, and abort or repair removes
     only the staged file, never the existing object. A path with no object
     keeps the direct write. The store is looked at before the reservation's
     transaction, so an object that appears in between is still written over
     (a small race; the complete then verifies what is there);
   - `overwrite` → see [overwrite](#overwrite).

   `404` for an unknown project or storage location; `422` for an invalid
   path, or a version group that matched a non-integer (naming the rule
   index and the value).
3. **Write** `write_path`. Only the reservation holder writes it. A
   source failure part-way through aborts the write instead of committing it
   (`autocommit=False` and `discard()` where the file object supports it; a
   best-effort abort helper in `storage/backends.py` otherwise).
4. **Complete:** `POST /uploads/{token}/complete`. The token must still be
   current (`409 {detail}` if it was taken over or already used). The server
   first *claims* the upload: the copy gets a fresh internal token, so a
   second complete, a renew or an abort racing it gets `409`. The final
   object is observed (stat → md5 → stat) and recorded under the guard:
   matching hash and size → `available`, else `unknown`; token cleared,
   generation bumped, latest recomputed. The response is the record (`200`).
   A staged reservation publishes instead (see above). If the object cannot
   be read (a transient store error), nothing is recorded: the claim is handed
   back, so the reservation stays owned by the client's token with its
   generation unchanged, and the answer is `503` (`Retry-After`); the client
   can retry complete, otherwise repair settles it after the deadline. An
   object that does not exist is observed (`unknown`), not an error.
   A complete whose token is no longer current gets `409`, except when a
   repair published exactly this reservation's content (the copy is now
   `available`, and the repair recorded the token in `published_token`; the
   next content-changing transition clears it): that is answered `200` with
   the record.

The client renews long transfers (`POST /uploads/{token}/renew`, every third
of the lease from a background thread: `200` with the new deadline, now +
`HELIOPUDDLE_OP_LEASE`; an expired reservation that nobody has taken over can
still be renewed). If a renew is refused, the write fails on its next read and
aborts without committing. Complete retries `503` responses carrying
`Retry-After` at most three times (four attempts in total), accepting either
delay seconds or an HTTP date, while keeping the reservation renewed during
the wait. Renewal is serialized against each complete request because its
server-side claim temporarily owns the token; that claim must not be mistaken
for a lost client lease. An unusable/missing retry header or any other error
is not retried. A lost lease interrupts the retry wait. On any failure after reserving (write, lost
reservation, complete) the client aborts (`DELETE /uploads/{token}`: `204`, or
`503` if the abort could not finish and is left to repair) and re-raises. An
aborted new-file upload is settled like an expired one
([expiry and repair](#expiry-and-repair)).
`POST /projects/{p}/files` registers a file that another tool has already
written: reserve and complete in one transaction, then `verify_file` inline (its function, `verify_file.fn`: no task run),
for a new path (`201`) or the same hash (`200`) only. A different hash is
`409` with the record, also with `overwrite: true`, whose detail then points
at `POST /projects/{p}/uploads`; a busy path is `409 busy`.

### API-based content transfers (M5A)

Readers and writers do not need backing-storage credentials. After reserving,
a writer can `PUT /uploads/{token}/content` with an octet-stream body. The API
spools the body to a temporary file with bounded memory, rejects data beyond the
reserved size (`413`), and rejects a short body or wrong md5 (`422`) before any
backend write. The temporary file is always closed, including on disconnect.
The server renews the reservation as chunks arrive during this receive phase.

The per-file write task claims the reservation (a concurrent PUT, complete or
abort then gets `409`, or a hidden `404` when a scoped caller's original token
no longer resolves), always writes a staging object, guards ownership and
renews the claim while copying, and hands the token back after writing (`204`).
New uploads are marked staged under the same row lock before writing, so
normal completion and expired-operation repair use the existing verified
publish path. A lost claim cannot commit or clean up another operation's object.
The client serializes renewals against PUT, as against complete; the server
renews while receiving and writing. Backend errors return a sanitized `503`;
the reservation can be retried or aborted. A normal complete still verifies
and publishes the bytes. No new queues, tables or background workers are added.

`GET /files/{id}/content` requires reader access on the file's project and
streams the first configured `available` or `overwriting` copy in chunks.
Unknown/deleted files, missing objects or no readable copy return `404`;
backend open/initial-read failures return sanitized `503`; a later read failure
terminates the stream rather than returning successful truncated content.
The opened stream is closed on EOF, error or disconnect. Responses are
`application/octet-stream`, `Cache-Control: no-store`, and never contain
backing-storage connection details. Client
`download` retains its existing hash check and atomic destination replacement.

## Overwrite

1. **Reserve** with `overwrite=true`: the `available` copy becomes
   `overwriting` with a token and `pending_hash`/`pending_size`, and keeps
   serving the old content. `write_path` is a unique staging path
   `<path>.<token>.hp-tmp`. The requested `metadata`, `content_date` and the
   user are kept on the copy (`pending_metadata`, `pending_content_date`,
   `pending_by`) and applied to the file only when the overwrite publishes; an
   abandoned or failed overwrite leaves the file unchanged. Only an `available` copy can be overwritten; any
   other copy state, or no copy in that storage location, is `409`: accept
   or remove the file first.
2. **Write** the staging path.
3. **Complete (publish):**
   1. verify the staged object's hash equals `pending_hash`;
   2. if the backend's move replaces its destination
      (`backends.move_replaces`): `fs.mv(staged, final)`; otherwise
      `fs.mv(final, <path>.<token>.hp-old.hp-tmp)` (a backup), then
      `fs.mv(staged, final)`, restoring the backup if that fails;
   3. re-verify the final object; on a mismatch restore the backup if there is
      one;
   4. guarded DB update: new hash and size, `updated_by`, `generation + 1`,
      token cleared, `available`, the file's other copies `unknown` unless an
      operation owns them, latest recomputed;
   5. delete the backup.

The upload is claimed first, as for a new file. At every step either the
complete original or the complete verified new content is at the final path.
The one exception needs two failures on a non-replacing backend: if both the
publish move and the restore fail, the original is complete in its backup
until repair moves it back. Any failure settles the copy from what the store
then holds (the same logic as repair): new content already at the final path
is recorded as published (`200`); otherwise the original is kept and the
copy returns to `available` (`409 {detail, file}`). If settling fails too,
the copy stays `overwriting` with an expired deadline and the next repair
finishes it. Staged content that does not match `pending_hash` and
`pending_size` is removed (`409`). The version is unchanged because the path
is unchanged.

## Delete

`DELETE /files/{id}` runs the `delete_file` task's function inline
(`delete_file.fn`, no Prefect task run; the `delete_files` flow runs it as a task):

1. Guarded: every copy that is not `deleted` becomes `deleting` with its own
   token (generation bumped), which blocks uploads to the path. A copy owned
   by a live operation makes the delete `409 busy`; an expired upload on a
   copy is repaired first; a `deleting` copy whose earlier delete did not
   finish is taken over.
2. Outside any transaction, each store object is deleted only if its current
   `info()` matches the copy's last observation (size, mtime when one was
   recorded, and a backend-reported md5 against the verified hash where both
   exist). Immediately before `rm` the delete re-confirms, in a short
   transaction under the series lock (`lock_copy` with its own token, the
   expected generation and state `deleting`), that it still owns the copy, and
   checks the object again. A delete that was taken over by a repair, or reset
   by a storage URL change, fails that check and removes nothing (the copy is
   reported `unknown`; its own guarded finish is dropped as stale). The gap
   between that check and the `rm` call cannot be closed on stores without a
   conditional delete: an object replaced inside it is still removed. It is
   the same race as an external writer replacing the object right after the
   check, and is as short as one store round trip. If it does not match, nothing is deleted from the store and the
   copy becomes `unknown`. A copy hp has never observed (no recorded size or
   `last_seen_at`, e.g. registered but not yet verified) is never deleted
   from the store; it too becomes `unknown`. An object that is already gone
   counts as deleted.
3. Guarded finish, each copy by its token: `deleted` (generation bumped),
   `unknown`, or — when the store delete failed — `deleting` without a
   token; the file is soft-deleted when every copy is `deleted`; latest
   recomputed.

Every expected outcome is `200` with the record: deleted; a store error
leaves the copy `deleting` (retried by the index or the next operation on
the path); a mismatch leaves it `unknown` with the file live. Unexpected
errors return `500`. The `delete_files` flow runs the same task over many
files (deployment `delete_files/delete-files`) and fails for any file not
`deleted`.

## Verify

`verify_file(copy_id, generation=None)` reads the object with one consistent
observation (stat → hash → stat; a change in between leaves the copy as it
is) and applies the result only if the copy's generation, live state and
expected hash are unchanged: hash **and** size (the file's registered size)
match → `available`; otherwise `unknown` (size and mtime recorded when the
object exists). An owned copy, or one that is `overwriting`, `deleting` or
`deleted`, is left alone, as is a call whose `generation` is not the current
one. Verify always reads the content, never a backend-reported md5. A
re-verification that finds the same state, hash, size and mtime writes
nothing (not even `last_seen_at`, which would bump `updated_at`) and does not
recompute latest, so a repeated verify moves no change key: a re-verify that
finds nothing changed writes nothing.

## Accept an unknown copy

`POST /files/{id}/accept {storage_location?}` accepts a copy in state
`unknown` as it is now:

1. Choose the copy and read its generation; observe the object (stat → md5
   by reading → stat; never a backend-reported md5).
2. Guarded update under the series lock, only if the copy is still `unknown`
   at that generation (`409` otherwise): the file's hash and size from the
   observation, the copy `available`, `updated_by`, `generation + 1`, latest
   recomputed. When the hash changed, the file's other copies become
   `unknown`. The write step is `server/models/accept.py`.

The storage location defaults to the file's only `unknown` copy. `404` for an
unknown or deleted file; `409` if the copy is not `unknown` or the object is
missing; `422` when no or several copies are `unknown`.

## Update metadata

`PATCH /files/{id}/metadata` with a JSON object: a shallow merge into the
existing metadata (a `null` value removes the key), or with `?replace=true` a
full replacement (`null` values are dropped). Sets `updated_by` and
`updated_at`. A deleted file's metadata cannot be changed (`404`).

## Update the content date

`PATCH /files/{id}/content-date {content_date}` sets a live file's content
date (a naive time is UTC); `null` goes back to the date the project's
version rules take from the path (a `date` group), or to none. Sets
`updated_by` and `updated_at` and notifies the rules, since rules can select
by content date. The version cannot be changed: it comes from the path
(API-02).

## Expiry and repair

Ownership expires at `op_deadline` (`HELIOPUDDLE_OP_LEASE`, default `1h`,
extended by renew and when an upload is claimed to complete). An expired
operation is repaired by the next full index, or by the next operation that
reserves, registers or deletes the path. The repair first takes the copy over
(a fresh token), then settles it from what the store holds:

- `pending` new upload: a final object matching the reserved hash and size is
  verified as normal (`available`). Otherwise, when the reservation created
  the copy, the object is removed and the copy row dropped (with the file row
  when it has no other copy); on an existing copy the object is left and the
  copy becomes `unknown` (`missing` without an object).
- `pending` staged (an object existed when it was reserved): the final object
  is the reserved content → recorded as published. Missing or failing
  verification with a backup present → the backup is restored. A staged file
  holding the reserved content → published (the existing object is backed up
  and restored if the move fails), only while the reserved hash and size still
  equal the logical file's accepted content. Otherwise the staged file is
  removed and the reservation dropped (it created the copy) or the copy
  becomes `unknown`; the existing object is never removed.
- `overwriting`: the final object decides. The new content there → recorded
  as published. The original there → the copy returns to `available`.
  Missing or wrong → a backup holding the original is restored, else a
  staged file holding the new content is published (only while the file still
  has the content the overwrite was based on, the copy's observed hash;
  another location may have replaced the file, and a stale overwrite must not
  undo that), else the copy becomes `unknown`. The path's staging and backup
  files are removed.

Staging and backup files are found by their exact name, `<path>.<uuid>.hp-tmp`
and `<path>.<uuid>.hp-old.hp-tmp`, with the token parsed and validated as a
UUID (`storage.location.parse_temp_path`). A longer sibling path's files
(`<path>.v2.<uuid>.hp-tmp`) never match, so repair, sweep and restore never
touch another path's operation.
- `deleting` (expired, or without a token): the guarded delete of that copy
  runs again. When a verified hash was recorded, repair also reads back that
  hash and checks it against the logical file before removal. This retains
  a move's source-content guard even if another writer preserved size and mtime.

After committing a `repaired` or `dropped` result, `repair_expired`
notifies matching rules once, without a storage-location restriction so
logical-content changes and latest-flipped siblings are considered. Dispatch
is best-effort and outside the repair transactions; live/no-op and incomplete
repairs do not notify. This hook belongs to repair, not to the shared
completion helpers or `index_batch`: listing can skip an owned copy whose
repair subsequently publishes it.

`services.upload` is a package: `results` (result types), `objects` (store
helpers), `settle_new` and `settle_overwrite` (settling an owned copy),
`complete` (complete, publish, abort), `repair` and `content`. Its public
names are re-exported from `services.upload`. The transfer service is split
the same way: `transfer` (tasks and store I/O), `transfer_support` (results and
decisions), `transfer_guards` (config and ownership guards) and `transfer_io`.

Helpers for the index: `models.uploads.find_expired(session,
storage_location_id, now) -> list[int]` lists the copies to repair, and
`services.upload.repair_expired(copy_id, *, now=None) -> RepairResult`
(`not_owned_or_live`, `repaired`, `dropped`, `incomplete`) repairs one. It
never raises on a store or config error: it returns `incomplete` and expires
the repair's own token, so the next run retries at once. A backup left behind
after a successful publish (its removal failed), and a staged file a client
wrote after its reservation was repaired, have no owner: the full index sweeps
them.

### Orphan sweep

The full index lists every `*.hp-tmp` file and, for each of the exact shapes
above, looks up the live file at the final path. If the copy in that storage
location holds an operation token (live, or expired and awaiting repair) the
file is left alone: the operation, or its repair, owns it (a claimed upload
holds a fresh token, so the client token in the name is not compared). Otherwise
it is an orphan: a staged file is removed; a backup is restored (the final
object removed first) if the final object is missing or its hash differs from
the file's recorded hash, else removed; with no live file at the path it is
removed. Files younger than `index.settle_time` are skipped, and incremental
runs never sweep. Counts: `swept` (removed or restored), and `skipped` for
files left. Sweep checks and store actions are not atomic: an operation that
reserves the path in between is not seen, but its files carry a new token, so
only the restore of an old backup can race it (negligible with the settle
time).

## Storage URL change

Changing a storage location's URL is treated as "the disk moved, with its
data". hp logs a loud warning; copies keep their hash and size but become
`pending` without a token (still latest-eligible) with observations cleared;
generations bump, tokens clear and index progress resets. That includes
`overwriting` copies, which return to their old content (`pending_hash` and
`pending_size` cleared; a staged `*.hp-tmp` object left in the new root is
never indexed), and
`missing` and `unknown` copies, which are looked for again. A `deleting` copy
stays `deleting` without a token, so the index retries the delete. Startup
fails if the new root is unusable. The first-start `index_full` then re-checks
each copy: matching content (hash and size) → `available`, different →
`unknown`, absent → `deleted`/`missing` as for any index. A delete never
removes an object hp has not observed in the current root.

## Copy / move between storage locations (M4, used by rules)

Rules use these operations both in their own runs and inside a
[reconcile](rules.md#reconcile-m5), which also re-runs them to a fixed point.

Options are per rule, with no configured-rule overrides except dry run.
One-off runs supply their own complete validated specification:

| Option | Default | Behaviour |
|---|---|---|
| `overwrite` | false | replace a destination with a different hash |
| `check_for_space` | true | check native free space and configured quota on the destination |
| `check_for_space_overhead` | 2GB | extra space required beyond the file size |
| `update_only_if_newer` | false | skip if the destination copy is newer than the source |
| `dry_run` | false | log only |
| `batch` | 100 files, 1GB, 30m | stop at the first limit after the current file |
| `retrigger` | true | schedule a successor if work remains |
| `pause_between_runs` | 30s | delay before a successor |

Steps for one file:

1. Snapshot the available source copy's generation and hash. Do not own the
   source for the whole transfer.
2. If the destination already has the same hash: verify by reading, mark
   `available` and write nothing (a move may then safely delete its source).
   If the destination copy is already `available` with exactly that
   observation (hash, size, mtime), there is nothing to record: the copy
   returns `unchanged`, with no DB write and no change notification (a move
   still deletes its source). If it has a different hash: if
   `update_only_if_newer` and the destination is newer, skip; else if
   `overwrite`, continue; else mark the destination `unknown` and report
   `conflict` (a dry run records nothing and reports `skipped`). Only when
   bytes will be written: if `check_for_space`, fail the file if free space <
   size + overhead, using the smaller native/quota value. An identical,
   newer or conflicting destination never fails on space. Repeating a conflict whose `unknown` copy already records the
   same observation writes nothing and notifies nothing (`skipped`). This is
   what lets a reconcile run again without changing anything.
3. Own the destination path with a renewable token ([ownership](#ownership))
   and copy, using generic `fs.copy` when `backends.py` confirms source and
   destination share a filesystem; otherwise stream. A new
   destination is written directly, an existing one through the
   [overwrite](#overwrite) protocol.
4. Verify the destination by reading it back (never a backend-reported md5).
5. Set the destination `file_storage_location` to `available`.
6. **Move only, and only now:** use the guarded single-copy delete with the
   source generation and hash captured at step 1. A source that changed
   meanwhile is never removed. The source copy records the verified
   destination copy (`move_destination_id`), and immediately before `rm` the
   destination is checked again in the DB and read back from its store.

If the source delete is interrupted (the process dies, or the store delete
fails and the copy stays `deleting`), the repair that retries it repeats that
destination check before `rm`. When the destination is no longer `available`
with the file's hash, or its object no longer reads back with that content,
the repair deletes nothing: the source copy becomes token-less `pending` and
the next full index re-hashes it. A repair therefore never removes the last
valid copy. Any other operation that takes the copy (a user delete, a URL
change) clears the recorded destination.

Any failure before step 6 leaves the source untouched and its state
unchanged. Expired destination tokens use the existing upload repair in the
full index. A stale transfer must not publish old content as the file's new
logical hash or clear another operation's token.

Outcomes: `copied`, `moved`, `deleted`, `unchanged`, `conflict` (a newly
recorded unknown destination, including one that appeared or was found newer
during the transfer), `skipped` (a newer or already recorded destination that
changed nothing) and,
in dry runs, `would_copy`, `would_move`, `would_delete`.

`transfer_file(file_id, source, destination, *, action="copy", options=None,
user=None)` is a Prefect task. It ensures both storage-location limits exist
and holds them in sorted order, including for standalone task calls. It
returns a `TransferResult` with `file_id`, `path`,
`outcome` and optional `detail`. Successful outcomes are `copied`, `moved`,
`skipped`, `would_copy` or `would_move`; failures raise, including an
incomplete source delete. There is no success-shaped fallback after a failed
phase. Dry runs neither reserve/repair copies nor create storage roots.

Destination ownership uses `pending` plus the upload protocol's
`pending_hash`, `pending_size` and `pending_staged`, with no new operation
table or discriminator. A background renewal runs every third of the lease;
a refused renewal stops streaming at the next read and prevents publication.
Generation, token, accepted hash/size and both endpoint URLs are checked again
before publication and completion. Failed attempts expire only their own
token for repair. A failed publish restores its original backup when one is
available; cleanup never clears another owner's token.

A transfer changes the destination copy, never the logical file's hash.
A simple same-content copy leaves the logical file's and source copy's
timestamps unchanged (unless latest truly flips), so incremental source rules
do not repeatedly select their own destination updates.

After destination completion commits, transfers notify matching rules before
attempting a move's source deletion. Copy notifications are restricted to
rules sourcing the destination, never the unchanged source. Destination-only
conflict observations use that same restriction. Moves and source-deletion
results notify without a source restriction; the control service includes
series siblings so latest flips are considered. Notifications happen outside
transactions, including when a later move phase fails. Dispatch failures are
logged and never fail the transfer; dry runs and skips without a committed
change do not notify.

Before removing a move's source, hp reads it back against the captured hash,
then owns only that expected source revision. Immediately before removal it
checks the source hash again, re-reads the destination object against the
captured hash (a store-side overwrite or removal since the read-back refuses
the delete) and guards both the source revision and the
verified destination's available revision. If the destination changed or
was removed, source deletion is refused and is not left as an automatically
retryable delete. A rule's byte budget counts source file sizes, not these extra reads.
The unavoidable check-to-store-action gap on backends
without conditional operations is the same one described under Delete.

## Delete (rule action)

The same guarded procedure as [delete](#delete), but only for the rule's
source copy. Other copies are untouched. Soft-delete the file only when no
live copies remain.

`delete_source_copy(file_id, source, *, expected_generation=None,
expected_hash=None, user=None)` is the source-only Prefect task. It holds
the source storage-location limit, checks optional expected revision/hash
before changing state, and returns a `TransferResult` with `deleted` only
after a guarded successful finish. A changed source, an unreadable object or
an incomplete removal raises instead.

## Rules and change notifications (M4)

`ignored_from_all_rules` excludes a file from all rules, not from indexing.
Changing it records `updated_by`; un-ignore updates `updated_at`.
Committed add/update operations (including latest flips and index batches)
notify matching enabled rules best-effort, outside the DB transaction.
Notification failures are logged and never fail a committed change.
Triggers, batching and one-off runs are described in [rules](rules.md).

## Flow run logs (SVC-30)

Every significant change a flow or task makes is logged through Prefect's run
logger, so it is shipped to the Prefect API and shown in the flow-run log view
of the Prefect UI at its default INFO level. Service modules get their logger
from one helper, `heliopuddle.server.services.run_logging.run_logger(__name__)`.
It returns the Prefect run logger while a flow or task run context exists and
the module's standard logger otherwise (the API calls a per-file task such as
`verify_file` inline, outside any run), and never raises
`MissingContextError`. One logger can therefore be created at import time and
used from both places.

| Level | What |
|---|---|
| INFO | `transfer_file`: `transfer <copy or move> of file <id> '<path>': <outcome>: <source> -> <destination>`; `delete_source_copy`; `delete_object` (`deleted <path> from storage location <sl>`); `delete_file` and `delete_files`; `verify_file` (`verified <project>/<path> in storage location <sl>: hash ok`); `verify_files` summary; upload completed, published and every repair result; accepted copy; index: a summary for each batch that added, updated, swept or failed anything, removals, and one summary line at the end of every run (listed, added, updated, removed, missing, unknown, repaired, skipped, bytes hashed) plus a note when a continuation is scheduled; the per-file and summary lines of rule runs and reconcile phases |
| WARNING | anomalies that leave things as they are: a verify mismatch or missing file (the copy becomes `unknown`), a delete that did not match or lost ownership, an invalid path in an index, a failed publish that is settled from the store |
| ERROR | failed tasks and files, with the exception for `logger.exception` |

An index run does not log every unchanged file, only the counts.
Messages name the project, path, storage locations, bytes and file id where
they are known, and never a storage URL or credential.

Prefect settings (read by the process that runs the flows, which is the hp
server or worker process, not the Prefect server): the default level is INFO
(`PREFECT_LOGGING_LEVEL`) and shipping to the API is on by default
(`PREFECT_LOGGING_TO_API_ENABLED=true`), so heliopuddle needs no logging
settings of its own. Prefect has no separate level for the API handler; it
follows the logger level. `heliopuddle.*` is deliberately not added to
`PREFECT_LOGGING_EXTRA_LOGGERS`: the helper already routes records to the run
logger, so doing so would only duplicate lines and ship the API's own logs.
Records emitted outside a run stay in the process log (see
`PREFECT_LOGGING_TO_API_WHEN_MISSING_FLOW` in the deployment doc).

When you host your own Prefect server (`PREFECT_API_URL` set), flows still run
in the heliopuddle `server start` processes, so set the logging variables there:

```bash
export PREFECT_API_URL=https://prefect.example/api
export PREFECT_LOGGING_LEVEL=INFO              # DEBUG for more detail
export PREFECT_LOGGING_TO_API_ENABLED=true     # ship run logs to the API (default)
export PREFECT_LOGGING_SETTINGS_PATH=/etc/heliopuddle/logging.yml  # optional custom config
heliopuddle server start
```

`PREFECT_LOGGING_EXTRA_LOGGERS=my.package` attaches further stdlib loggers
(for example your own code in a pipeline) to the run logs;
`PREFECT_LOGGING_SETTINGS_PATH` points at a Prefect `logging.yml` to replace
the handlers. If a server-wide profile or a worker's environment sets these
differently, the values of the process running the flow win.


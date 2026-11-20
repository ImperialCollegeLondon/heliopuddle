# Indexing (M3-M4)

Indexing keeps the DB in step with what is actually in each storage location.
It is how existing folders and buckets are adopted and how changes made
directly in the store are picked up.

## Configuration

```yaml
index:
  full_schedule: "0 3 * * *"          # cron; omit for on-demand only
  incremental_schedule: "*/10 * * * *"
  batch_size: 1                       # listed entries per index_batch task
  batch: {max_files: 100, max_bytes: 1GB, max_duration: 30m}
  retrigger: true
  pause_between_runs: 30s
  settle_time: 60s                    # skip files modified more recently
  reverify_after: 1h                  # stale pending/deleting copies are repaired
  lock_timeout: 1h                    # how long a full index waits for the lock
  incremental_overlap: 5m             # re-check files just before the watermark
  incremental_lookback: 2d            # days expanded into incremental_prefixes
  incremental_prefixes:               # optional; default: whole storage location
    - "science/{date:%Y/%m/%d}"
```

All keys are optional. The config is validated at startup (DEP-07). Each
`incremental_prefixes` template is formatted once at load time (format syntax
and strftime, DEP-12): an unmatched brace, a placeholder other than
`{date:<strftime>}`, or a template that would expand to an absolute path or
outside the storage location stops startup with the file and field named.

## Flows and tasks

- **`index_full(project, storage_location)`** streams a full listing
  (directory by directory, see [scale](#correctness-and-scale-rules)) and
  diffs it against the storage location's copies. It is the only flow that
  detects removals. It also repairs expired operations and stale copies
  (below).
- **`index_incremental(project, storage_location, since=None)`** considers
  files whose mtime is after `since` (default: the
  `index_progress.last_incremental_at` watermark) minus
  `incremental_overlap`, changed DB files/copies, and previously unindexed
  store paths even when their mtime is older. If `incremental_prefixes` is set, only those prefixes
  are listed: each template is expanded for every day from now back to
  `incremental_lookback` (e.g. `science/2026/10/28`, `science/2026/10/27`,
  ...), and prefixes that do not exist are ignored. Removals are not detected.
- **`index_batch(project, storage_location, entries, mode, listed_at)`** (task)
  applies the diff rules to up to `batch_size` listed entries. The flow runs
  one task per batch, so the Prefect UI shows progress and failures per batch.
  Each object is observed (stat, hash, stat) inside the task, outside any DB
  transaction; each file's DB change is one guarded write transaction under
  its series lock, which expects the copy's generation and checks that the DB
  still holds what the task read (otherwise the file is skipped and looked at
  again next run). Every listed copy gets `last_seen_at`, in one statement per
  batch. Every entry is attempted; a batch with a failed entry fails after the
  rest, naming the paths.
- **`index_removals(project, storage_location, candidates, listed_at)`** (task,
  full index only) records copies that the listing did not see, in batches of
  `batch_size`. Each candidate carries its copy id, generation and path, and
  is checked again in the store first (`info()`: only "not found" counts as
  gone, any other error fails the task): one that exists after all is skipped.

The listing streams `StorageLocation.list_files(prefix)`. An incremental run
drops temp files (counted as `skipped`); a full run passes them to
`index_batch` marked `temp`, which sweeps the orphans (see [operations](operations.md#orphan-sweep);
counted as `swept`, or `skipped` when an operation owns them or they are inside
the settle time). The flow drops files inside the settle time (counted as
`skipped`; a full index still records them as seen) and, for an incremental
run, files at or before the watermark minus the overlap (not counted) before
batching. If any batch or removal fails, or a repair does not complete, the
flow fails at the end: completed-chain watermarks do not advance, and the
cursor never advances past a failed task. Earlier completed batches keep
their saved progress. An incremental run
with no watermark (and no `since`) processes every file.
`incremental_prefixes` are expanded per UTC day; duplicate prefixes and
prefixes inside another one are dropped; a template without `{date:...}` is
listed as is.

Both flows hold `hp_concurrency("storage-location:<project>:<storage_location>")`:
the full index waits up to `lock_timeout` (then fails with a timeout); the
incremental index skips at once if the lock is busy and returns a summary with
`skipped_run: true`. The flows create the limit (one slot) if it does not exist. On success
they update `index_progress`: `last_full_at` = the time the listing started;
`last_incremental_at` = the time the listing started minus `settle_time`
(below).

## Correctness and scale rules

### Bounded runs and continuation (M4)

`batch_size` remains the number of entries per Prefect task. Independently,
`index.batch` limits the entire run by completed files, bytes actually read
for hashing (native hashes and unchanged entries cost zero bytes), and
elapsed duration. Defaults are 100, `1GB` and `30m`; stop at the first reached
limit after the current file, including when a task's configured batch size
would otherwise exceed it.

Persist progress after each completed batch. Continuations preserve the
listing cursor, selection window and chain start; incremental DB candidates
use `(updated_at, id)` keysets, so ties never disappear at a batch boundary,
read through the indexed two-stream merge described in [rules](rules.md).
The streamed store listing also retains its path cursor; unindexed entries
must not be discarded because they have no DB id yet.
The DB phase uses the later file/copy update, an inclusive lower time bound
and a strict two-part cursor, bounded above by the original chain start.
The listing phase discovers objects absent from that snapshot. Its cursor
compares directory components with files before subdirectories, not a global
lexicographic path order; configured prefixes use that same traversal order.
A removed cursor path does not prevent resume. Bounded listing pages join
DB observations in bulk, and the final re-query also finds new paths that
appeared before the cursor. Successful observations are recognised through
`last_seen_at`, not an in-memory set of the whole tree.

Re-query for remaining work before finishing. If work remains and `retrigger`
is true, submit the same deployment scheduled `pause_between_runs` later
(default `30s`) with the cursor. With retrigger false, saved progress remains
available to the next manual/scheduled run. Failed work is not advanced past.
A full chain uses its original start for all `last_seen_at` updates and
performs removals only after the final listing batch, never between runs.
Only a completed full chain advances `last_full_at`; the safe incremental
watermark retains the settle-time rule.
Removal and repair work also consumes the run's limits. After the listing
finishes, full-chain cursors identify the expired-operation, removal,
pending-verify or delete-repair phase and its last completed copy id. No
removal phase can begin while listing work remains. Hash-byte accounting
measures actual reader output, including discarded observations and repair
hashes; neither native hashes nor listed/unchanged sizes consume bytes.
Each guarded read-back during delete repair counts, including repeated reads
of the same source bytes.

The internal `continuation` Prefect parameter carries a checkpoint, but the
DB checkpoint is authoritative. A stale scheduled continuation cannot
restart an already-completed or replaced chain. With retrigger disabled,
the next manual run reads the same saved checkpoint.
Checkpoint and completion writes also check the active storage location's
URL under a row lock. An old in-flight chain cannot resurrect progress that
a concurrent storage URL change reset; it fails clearly and leaves the new
root to adoption.

Committed batches that add or change files best-effort trigger matching rules;
indexing continues to track files marked ignored from rules.
`index_batch` returns `IndexBatchResult.changed_file_ids` after its writes
commit, including latest-flag flips in affected series. Once all batch
transactions close, it calls
`rule_control.notify_file_changes(ctx, project, changed_file_ids)` once,
without restricting the source storage location: latest flips can affect
rules on other sources too. Unchanged batches do not notify. Committed
changes are still notified when another entry failed, before the task
reports that failure. Notification failures are logged and never fail or
roll back indexing.

### Existing invariants

- **Ownership:** copies owned by another operation (a token whose deadline
  has not passed, see [operations](operations.md)) are skipped, whatever their
  state, as are `overwriting` and `deleting` copies; `*.hp-tmp` objects are
  skipped when an operation owns the path and otherwise swept by the full index;
  expired operations are repaired by the full index. Copies with a token are
  never removal candidates.
- **One observation per object:** stat (it must match the listing) → hash →
  stat; if size or mtime changed meanwhile, or the object is gone, it is
  skipped until the next run. A backend md5 reported by the listing itself is
  one observation with the listed size and mtime. Size, mtime and hash stored
  on a copy always describe the same observation. Every change is applied
  with the copy's expected `generation` (through the guarded-transition
  helper); every state or hash change increments it.
- **Safe watermark:** an incremental run saves `listing start − settle_time`
  as its watermark, so files skipped while settling are processed by a later
  run whatever `incremental_overlap` is.
- **Streaming:** listing walks one directory at a time (`fs.ls(detail=True)`
  per directory, as `fs.walk(detail=True)` does; `fs.walk` itself is not used
  because it silently omits subdirectories it fails to list). It never builds
  the whole tree in memory or sorts it: the files of a directory, then its
  subdirectories, are visited in name order. A directory that vanishes
  meanwhile is skipped; any other listing error fails the run. Removal
  detection is set-free: listed copies get `last_seen_at = listing start`;
  after the listing, copies of that storage location with an older (or no)
  `last_seen_at`, not changed since the listing started and without a token,
  are read in keyset pages of 1000 and checked by `index_removals`.
- **Repair results are checked:** an incomplete repair (a delete still
  `deleting`, a verify that crashed while the copy still awaits verification,
  an expired operation not repaired) is counted as `repair_failed`, the run
  fails and progress is not advanced.
- **Deployments** are named `index-<mode>-p<project id>-sl<storage location
  id>-<project>-<storage location>` (collision-free; duplicate identities are
  rejected before registration); submissions pass explicit parameters;
  adoption uses a Prefect idempotency key so racing starts create one run
  (below).

## Diff rules

For each listed file, compared with its copy in this storage location:

| Situation | `accept_external_changes: true` | `false` |
|---|---|---|
| new (no live file at the path) | add: version from path rules, md5, copy `available` | add file with copy `unknown` |
| live file exists, no copy here | add copy, hashed; same hash → `available`, else `unknown` | copy `unknown` |
| same size and mtime | update `last_seen_at` | same |
| size or mtime changed | re-hash; hash changed → update file hash/size, copy `available` | copy `unknown` |
| in DB, not in store (full only) | copy `deleted`; soft-delete the file if no live copies remain | copy `missing` |

Details:

- "No copy here" includes a copy in state `deleted` (the row is reused).
- A `missing` copy that is listed again is treated like a changed one.
- A `pending` copy without a token that was never observed in this root (no
  `mtime`, no `last_seen_at`: what a storage URL change leaves, see
  [operations](operations.md)) is re-checked whenever it is listed: the
  object is read and compared with the file's hash and size (match →
  `available`, else `unknown`), in accepting and non-accepting storage
  locations alike. Counted as `reverified`. When a full index does not list
  it, it is gone as above (`deleted`/`missing`).
- Other `pending` copies, and `overwriting` and `deleting` copies, are never
  diffed (skipped); the full index repairs them (below).
- New files are hashed in both modes, so the file row has a hash (a client
  adding the same content later then matches it).
- A new file whose version rule extracts an invalid version (DEP-12), or a
  version of the other kind than its series holds (API-21), is recorded as
  unversioned in its literal-path series with an `unknown` copy (never
  latest); the reason is logged as a warning and the run goes on. A copy recorded as
  `unknown` keeps the listed size and mtime (but no hash), so an unchanged
  unknown copy is not re-reported on the next run.
- When accepting a changed hash, the file's other `pending`, `available` or
  `missing` copies become `unknown`, as for an overwrite.
- A path that is not a valid relative path (e.g. contains `..` segments) is
  skipped with a warning.

Files written by the indexer get `created_by`/`updated_by` =
`heliopuddle:index`. Files never become latest unless they have a `pending` or
`available` copy (see [versioning](versioning.md)), so unknown files are
visible but never latest. An operator can accept any `unknown` copy (see
[operations](operations.md)).

Skipped:

- `*.hp-tmp` files that an operation owns, or that are inside the settle
  time (the full index sweeps the orphans; the incremental index never does);
- files modified within `settle_time` (picked up next run; avoids racing a
  client that has written a file but not registered it yet);
- copies owned by an operation, `overwriting` and `deleting` copies, and
  token-less `pending` copies already observed (repaired once older than
  `reverify_after`).

## Hashing

md5, computed by hp. Where the listing reports a backend md5 for an object
(see [storage](storage.md)), the indexer uses it instead of reading the object;
otherwise it streams and hashes the object. Verify and accept always read.

## Repair (full index only)

- Expired operations (a token whose `op_deadline` has passed) → the
  operation's own repair (`repair_expired`, see "Expiry and repair" in
  [operations](operations.md)); counted as `repaired`, or `repair_failed`
  when it reports the repair incomplete or raises.
- Token-less `pending` copies older than `reverify_after` → `verify_file`.
  Counted as `reverified`; a crash, or a dropped result while the copy still
  awaits verification, is `repair_failed`.
- Token-less `deleting` copies older than `reverify_after` → `delete_file`
  (retry, with `updated_by` = `heliopuddle:index`). Counted as `reverified`
  when the file is deleted; a delete still `deleting`, or a crash, is
  `repair_failed`.

"Older" is by the copy's `updated_at`. Any `repair_failed` fails the run after
everything else was attempted, and progress is not advanced.

## Run summary

Each flow returns an `IndexSummary` and publishes it as a Prefect markdown
artifact with key `index-<mode>-<project>-<storage_location>` (lower-cased;
characters other than letters, digits and `-` become `-`):

| Count | Meaning |
|---|---|
| `listed` | files listed in the store, including skipped ones |
| `added` | new files, and new copies of known files |
| `changed` | files whose accepted content (hash) changed |
| `removed` | copies found gone and marked `deleted` |
| `missing` | copies found gone and marked `missing` |
| `unknown` | copies newly recorded as `unknown` |
| `swept` | orphaned `*.hp-tmp` staging and backup files removed, or restored from a backup (full index) |
| `skipped` | temp files left (owned, or inside the settle time; every temp file in an incremental run), files inside the settle time, owned, `pending`, `overwriting` and `deleting` copies, invalid paths, files that changed while being indexed, removal candidates found after all |
| `reverified` | `pending` copies re-checked or re-verified, and stale `deleting` copies deleted |
| `repaired` | expired operations repaired |
| `repair_failed` | repairs that did not complete (the run fails) |
| `processed` | completed/attempted files counted against the run limit, including removal and repair work |
| `hashed_bytes` | content bytes actually read for hashing, including reads whose observation was discarded |

plus `project`, `storage_location`, `mode`, `started_at` (the listing start)
and `skipped_run` for an incremental run that did not get the lock or a stale
continuation, and `remaining` when a saved chain still has work. Index flows
retain their summary artifacts; the no-artifact rule applies only to rule
flows.

## Deployments and adoption

For every active storage location hp serves
`index-full-p<project id>-sl<sl id>-<project>-<sl>` and
`index-incremental-p<project id>-sl<sl id>-<project>-<sl>` (the ids make the
names collision-free), scheduled from `full_schedule` and
`incremental_schedule`, with parameters `{project, storage_location}` and a
deployment concurrency limit of 1. Index deployments that are no longer in
config are deleted from Prefect on start, and the
`storage-location:<project>:<sl>` global concurrency limits are created (limit
1; a creation race with another process is resolved by re-reading). After the
services are serving, hp submits an `index_full` run (tag
`heliopuddle-adoption`, explicit parameters, idempotency key
`adopt-sl<sl id>-g<n>`) for each active storage location that has never been
fully indexed: this adopts an existing folder or bucket without any client.
An unfinished adoption run's key is reused; otherwise `n` is one more than the
highest earlier one, so processes starting together create one run. Details in
[deployment](deployment.md#index-deployments-m3-svc-09).

On demand: `POST /projects/{p}/runs {kind: "index", storage_location, mode}`,
the client's `trigger_index`, or "Index now" in the UI. A
[reconcile](rules.md#reconcile-m5) runs `index_full` for every storage
location as its first phase, looping the batches inline.

## Code layout

The flows live in `server/services/index.py` (`index_full`,
`index_incremental`, `index_full_chain`) and re-export the public index names.
The pieces: `index_models.py` (entries, counts, summaries), `index_diff.py`
(observe one object and apply one guarded change), `index_tasks.py` (the
`index_batch` and `index_removals` tasks), `index_run.py` (listing, change
candidates and checkpoint of one run), `index_finish.py` (removals and repair
at the end of a full index), `index_names.py` and `index_sweep.py`.

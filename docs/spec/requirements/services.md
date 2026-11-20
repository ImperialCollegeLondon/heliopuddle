# Services requirements (`SVC`)

**SVC-01 (M1)** `server start` registers and serves the services' deployments
(`verify_files` in M1) with `prefect.serve()`. When `PREFECT_API_URL` is
unset, it starts an embedded Prefect server child process (`PREFECT_HOME`
under the data dir), waits for `/health`, and stops it on exit. When
`PREFECT_API_URL` is set and `{url}/health` fails, startup exits non-zero with
a clear message.
*Verify:* integration tests for both cases.

**SVC-02 (M1)** The Prefect task `verify_file(file_storage_location_id)` re-hashes
the copy in the store. On a match with the registered hash it sets `available`
and records size, mtime, hash and `last_seen_at`; on a mismatch or a missing
file it sets `unknown`. The API calls it inline during register. The flow
`verify_files(file_storage_location_ids)` runs the same task for many files
and is a served deployment. Given the same copy, both paths give the same
result.
*Verify:* unit tests for the task (match, mismatch, missing) under
`prefect_test_harness`; the flow over several files; integration test
submitting the deployment.

**SVC-07 (M2)** The Prefect task `delete_file(file_id)` marks every copy
`deleting`, deletes each from its storage location (an already-missing object
counts as deleted), marks copies `deleted`, sets `file.deleted_at` and
`updated_by`, and recomputes `is_latest`. If a store delete fails the copy
stays `deleting`. The flow `delete_files(file_ids)` runs the same task over
many files and is served as the deployment `delete_files/delete-files`.
*Verify:* unit tests for the task and flow; integration test submitting the
deployment.

**SVC-03 (M3)** `index_full(project, storage_location)` streams a listing of the
whole storage location and diffs it against the DB, processing entries in an
`index_batch` task per `index.batch_size` entries (default 1). It is the only
flow that detects removals. With `accept_external_changes: true`: a new file
is added (version from the path rules, md5 hashed — native md5 where the
backend has one — copy `available`, `created_by` `heliopuddle:index`); same
size and mtime → `last_seen_at` updated; size or mtime changed → re-hashed and,
if the hash changed, the file's hash and size are updated (copy stays
`available`); a file in the DB but gone from the store → copy `deleted`, the
file soft-deleted when no live copies remain, and latest recomputed. With
`false`: new → recorded with an `unknown` copy, changed → `unknown`, gone →
`missing`. Skipped: `*.hp-tmp` owned by an operation (orphans are swept by the full index), files modified within `index.settle_time`, and
`pending`/`deleting` copies younger than `index.reverify_after`. Adopting an
existing tree gives every file the right version, series and latest.
A backend-reported md5 is only an indexing shortcut to avoid reading objects;
`verify_file` and accept always read the content.
*Verify:* unit tests on `file://` for every row of the diff table in both
modes with batch sizes 1 and 3; a forged native md5 is caught by verify and
accept; integration tests on Azurite.

**SVC-08 (M3)** `index_incremental(project, storage_location, since=None)`
processes only files whose mtime is after the watermark
(`index_progress.last_incremental_at`, or `since`) minus
`index.incremental_overlap`. Listing is limited to `index.incremental_prefixes`
when set (strftime templates such as `science/{date:%Y/%m/%d}`, expanded for
each day in `index.incremental_lookback`), else the whole storage location. It
applies the same add and change rules as the full index, never marks anything
removed, and advances the watermark only on success.
*Verify:* unit tests: watermark, overlap, prefix templates and lookback, no
removals.

**SVC-09 (M3)** For every active storage location, hp serves the deployments
`index-full-p<project id>-sl<storage location id>-<project>-<storage_location>` and
`index-incremental-p<project id>-sl<storage location id>-<project>-<storage_location>`, scheduled from the project's
`index.full_schedule` / `index.incremental_schedule` (cron, optional). Index
deployments no longer in config are deleted from Prefect on start. On first
start, an `index_full` run is submitted for each active storage location with
no `last_full_at` (adoption). Both flows hold
`hp_concurrency("storage-location:<p>:<sl>")`: a full index waits up to
`index.lock_timeout`; an incremental index skips at once if the lock is busy.
Each run returns counts (listed, added, changed, removed, missing, unknown,
skipped, reverified) and publishes them as a Prefect markdown artifact; index
progress is saved only on success.
*Verify:* unit tests (deployment specs, schedules); integration tests with
embedded Prefect (deployments and schedules registered, adoption completes on
start, an incremental run skips while a full run holds the lock).

**SVC-10 (M3)** A full index re-verifies `pending` copies older than
`index.reverify_after` with `verify_file`, and retries `deleting` copies older
than the same threshold with `delete_file`.
*Verify:* unit tests.

**SVC-11 (M3)** Verify results are applied only if the copy's generation,
live state and expected hash are unchanged since the verify started
([operations](../../design/operations.md)).
*Verify:* interleaving tests: verify vs overwrite, verify vs delete.

**SVC-12 (M3)** Changing a storage location's URL logs a loud warning; copies
keep their hash and size but become `pending` (no token) with observations
cleared, generations bump and index progress resets; startup fails if the new
root is unusable; the adoption full index re-checks each copy (match →
`available`, mismatch → `unknown`, absent → `deleted`/`missing`).
*Verify:* unit and integration tests.

**SVC-13 (M3)** Index deployment names include the project and storage-location
ids (`index-full-p<id>-sl<id>-<project>-<sl>`); duplicate identities are
rejected before registration; API and adoption submissions pass explicit
parameters.
*Verify:* unit test with `alpha-beta`/`primary` and `alpha`/`beta-primary`;
integration test.

**SVC-14 (M3)** The incremental watermark saved is the listing start minus
`settle_time`, so files skipped while settling are processed by a later run.
*Verify:* unit test with `settle_time` greater than the overlap.

**SVC-15 (M3)** Failure semantics: an expected incomplete delete returns `200`
with state `deleting`; an unexpected delete error returns `500`; index repair
results are inspected — an incomplete repair is counted as `repair_failed`, the
run fails and index progress is not advanced.
*Verify:* unit tests.

**SVC-16 (M3)** Observations are consistent: verify checks registered size,
stored size and hash together (any mismatch → `unknown`); the indexer stats,
hashes, then stats again and skips a file that changed meanwhile, so size,
mtime and hash always describe one observation.
*Verify:* unit tests (size-mismatch register; file changing during hashing).

**SVC-17 (M3)** Index scale: listing streams one directory at a time (`fs.ls(detail=True)`, as `fs.walk(detail=True)` does) without
building the whole tree in memory or sorting it; removals are detected without
an in-memory path set (listed copies get `last_seen_at` = listing start;
removals are older ones); `recompute_latest` selects the top eligible version
in SQL without loading every member.
*Verify:* scale tests (e.g. 20k files, a 2k-version series) with time bounds.

**SVC-18 (M3)** Concurrent first starts are idempotent: concurrency-limit
creation tolerates conflicts by re-reading; adoption runs are submitted with a
Prefect idempotency key per storage location and generation, so racing starts
create one run.
*Verify:* unit tests with racing probes; integration test with two processes.

**SVC-04 (M4, amended M6A)** Match criteria select exactly the expected
files: include/exclude globs; `is_latest` (`null` every version, `true` latest
only, `false` all but the latest); ignored-file exclusion; and `age`, any
combination of `modified`, `created`, `content_date` and `arrived`, each with
`older_than` and/or `younger_than` given as a duration or an absolute ISO
date/time, all of which must hold. `modified` is the source copy's mtime,
falling back to when hp first recorded the file; `created` is when hp first
recorded the file; a missing `content_date` never matches; `arrived` is the
source copy's `arrived_at` (SVC-29). SQL pre-filters available source copies,
latest, every age bound, escaped glob prefixes and not ignored; Python then
matches globs exactly. The removed `older_than`, `age_by`,
`content_date_after` and `content_date_before` match keys are rejected.
*Verify:* table-driven golden-dataset tests over every field, both bounds,
ranges, absolute dates and combinations.

**SVC-05 (M4, amended M6A)** Incremental runs select new/changed files or
copies, latest flips and files that crossed any relative `older_than` bound
(on any age field) since the previous selection window; a first incremental
run behaves as full. Full mode
evaluates the whole index. Latest flips update each changed file's timestamp.
Rule and storage-location limits prevent overlapping rule runs/transfers.
*Verify:* unit selection tests and integration tests with Prefect.

**SVC-19 (M4)** `transfer_file` checks space, decides whether the destination
can be written, owns it with a renewable token, writes directly only when
absent (otherwise stages/publishes), reads back to verify, and marks available
under the guard. Move deletes only the original source generation/hash.
Same-filesystem copies use `fs.copy` when the backend capability allows it;
others stream. Expired tokens are repaired by full indexing.
*Verify:* task tests and M3-style interleavings against uploads, overwrites,
deletes, index and storage URL changes.

**SVC-20 (M4)** A delete rule removes only the source storage-location copy
through STO-13's guarded delete. The file is soft-deleted only when no live
copies remain.
*Verify:* multi-copy task tests and guarded-delete interleavings.

**SVC-21 (M4)** `run_rule` serves deployments named
`rule-p<pid>-<project>-<rule>`, cron or on-demand. Reject duplicates and remove
stale deployments on start. Hold `hp_concurrency("rule:<p>:<name>")` and both
transfer storage-location limits; deployment concurrency is 1, `ENQUEUE`, so
a continuation or queued follow-up waits for the slot instead of being
cancelled.
Counts and per-file outcomes go to flow-run logs, with no rule artifact.
Any failed file fails the run. Dry run logs `would_*` and changes neither
store nor DB.
*Verify:* unit flow/deployment tests and real Prefect integration tests.

**SVC-22 (M4)** `(updated_at, id)` keyset watermarks never skip files sharing
a timestamp, including across batch and retrigger boundaries in rules and
incremental index runs.
*Verify:* more timestamp-tied files than the batch limit, including 250 files
through three 100-file batches in the fast unit regressions. The live
250-file acceptance scenario passed on `03d3393`; the owner accepted that
evidence and retired the slow integration test.

**SVC-23 (M4)** Every rule/index run stops after the current file at the first
files/bytes/duration limit (defaults 100/1GB/30m). Rule limits are per rule;
index limits are per project, counting bytes only when read for hashing.
Save completed-batch progress. Re-query for work and schedule the same
deployment after `pause_between_runs` (30s), unless retrigger is off or the
rule disabled. A full-index chain retains its start and detects removals
only in its final batch.
*Verify:* each limit separately and combined, continuation/restart tests,
late-arriving work, no premature removals.

**SVC-24 (M4)** Trigger is a successful no-op while a scheduled or pending
run exists (it has not selected its work yet; future runs the Prefect
scheduler created for the cron do not count). While only a running run
exists it queues exactly one follow-up incremental run, keyed to the running
run; otherwise it starts one. Racing triggers create at most one run. Committed file changes (upload/register/publish,
transfer publication, source deletion, expired-operation repair, accept,
metadata, un-ignore, index batches and latest flips) best-effort
trigger matching enabled rules; failures are logged, never fail the change.
*Verify:* post-commit notification and failure-isolation unit tests; racing
triggers and downstream rules after transfer/repair against real Prefect.

**SVC-25 (M4)** A rule runs only when enabled in both YAML and DB.
Disabling pauses its schedule, cancels future scheduled runs and prevents
retrigger; an active batch finishes. Enabling resumes the schedule.
Disabled manual submissions/triggers return `409 disabled`.
*Verify:* unit tests and external Prefect schedule/cancellation integration.

**SVC-06 (M5)** Reconcile is idempotent: with no external change in between,
a second run makes no changes (every C5 count is zero) and the store and DB
are identical before and after it, including when the rules are listed in an
order that needs more than one pass.
*Verify:* unit tests with `flow.fn`; integration test against real Prefect.

**SVC-26 (M5)** `reconcile(project, verify=False, rules=None)` runs the phases
in order with inline chains: `index_full` for every active storage location,
the optional verify of every `available` copy (a mismatch becomes `unknown`),
then the enabled rules in full mode, limited to `rules` when given (unknown
names rejected, named disabled rules skipped and logged). The rule phase
repeats until a pass changes nothing, up to `reconcile.max_rule_passes`;
otherwise the run fails naming the rules still changing things. It holds
`hp_concurrency("reconcile:<p>")` and each batch's existing limits, and logs
per-phase and per-rule change counts.
*Verify:* unit tests: an order needing two passes, a non-converging pair,
corruption found only with verify, a subset leaving other rules' files alone.

**SVC-27 (M5)** Each project has one reconcile deployment,
`reconcile/reconcile-p<pid>-<project>`, with the optional cron from
`reconcile.schedule` and the configured `verify` and `rules` as parameters.
Deployment concurrency is 1 with `ENQUEUE`; duplicates are rejected and stale
reconcile deployments removed on start.
*Verify:* unit test of the built deployments; integration test of registration.

**SVC-28 (M5)** A rule pass inside reconcile continues past failed files:
each failure is recorded and the pass completes the remaining files and
rules, then the reconcile fails listing every failed file with its rule and
error. Failures reaching `reconcile.max_failures` (default 5) fail the run at
once. No further pass starts after any failure. A failed index or verify
batch, or an incomplete repair, fails the reconcile immediately.
*Verify:* unit tests with a locked file and with the failure limit reached.

**SVC-29 (M6A)** **Arrival time.** `file_storage_location.arrived_at` is set
when a copy is first recorded in a storage location (upload, register, index
add) and set again whenever a copy or move rule transfers the file into that
location, including a move onto identical content already there. The reset
bumps the copy's generation, so an operation selected before it (such as a
scheduled delete) is stale and does not run. Verify, re-index, accept,
overwrite and metadata or content-date changes leave it alone. After a one-off move into a location, a scheduled rule
on that location with `arrived: {older_than: 1d}` does not select the file
until a day has passed, in full and incremental runs.
*Verify:* unit tests per write path; a one-off move then scheduled-rule test
with a controlled clock.

**SVC-30 (M6A)** **Flow run logs.** Every significant system change made by a
flow or task emits at least one INFO record through Prefect's run logger, so
it is shipped to the Prefect API and visible in the flow-run log view at the
default level: a transfer (copy or move) naming the file and both storage
locations, a source copy deleted, a file deleted, a verification (hash ok at
INFO; mismatch or missing at WARNING), an upload completed, published or
repaired, an accepted copy, and an index run (a summary per batch that changed
something and one at the end of every run, never a line per unchanged file).
Anomalies are WARNING and failures ERROR. Service code outside a run (the API
calling a per-file task inline) falls back to the standard logger and never
raises. Records carry no credentials.
*Verify:* unit tests asserting the INFO records of a transfer, a delete, a
verify (ok and mismatch) and an index run (and none per unchanged file); a test
that the helper uses the standard logger outside a run and the Prefect run
logger inside one.


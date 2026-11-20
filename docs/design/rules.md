# Archiving rules (M4)

Rules are validated, read-only YAML config. Each configured rule is a
deployment of `run_rule`; runtime enablement and progress live in `rule_state`,
not in a custom queue or run table. See the approved
[M4 decisions](../spec/reviews/m4-plan.md).

```yaml
storage_locations:
  - name: primary
    url: file:///data/primary
  - name: archive
    url: file:///data/archive
    quota: 500GB
rules:
  - name: archive-superseded
    action: move
    source: primary
    destination: archive
    match:
      paths: ["data/**/*.dat"]
      exclude: []
      is_latest: false            # null: all, true: latest only, false: all but latest
      age:                        # every criterion listed must hold
        modified: {older_than: 30d}
        content_date: {older_than: 2025-01-01T00:00:00}
        arrived: {older_than: 1d} # cooling-off after arriving in the source
    options:
      overwrite: false
      check_for_space: true
      check_for_space_overhead: 2GB
      update_only_if_newer: false
      dry_run: false
      retrigger: true
      pause_between_runs: 30s
      batch: {max_files: 100, max_bytes: 1GB, max_duration: 30m}
    enabled: true
    schedule: "17 2 * * *"
```

Only `name`, `action`, `source`, `match.paths`, and `destination` for copy/move
are required. The sample shows the option defaults. Match filters default to
unrestricted, except paths.
`schedule` is optional (on-demand only). Sizes use decimal units (`1GB`,
`500MB`) or explicit binary units (`1GiB`); durations use the existing
human-readable syntax (see [Durations](#durations)), preserving fractional seconds such as `0.5s` when
serialized into Prefect run parameters. Batch limits are independently optional but at least
one must remain set, and every set limit must be positive.

Startup rejects duplicate rule names, missing storage-location references,
equal source/destination, a destination for delete, invalid relative globs,
durations, sizes, absolute dates, age ranges that can never match, or cron.
The same models validate one-off specs. Configured rules have no per-run
config overrides.

## Age criteria

`match.age` selects files by up to four dates. Each takes `older_than`,
`younger_than` or both (a range); every criterion listed must hold:

| Field | Date | Missing |
|---|---|---|
| `modified` | the source copy's mtime, else when hp first recorded the file | never |
| `created` | when hp first recorded the file (`file.created_at`); storage backends do not reliably report a birth time | never |
| `content_date` | the file's content date | never matches |
| `arrived` | when the file arrived in the source storage location (`file_storage_location.arrived_at`) | never |

A bound is a duration (`3d`, `1h30m`, `1y6mo`), measured back from the run's
selection time, or an absolute ISO date/time (UTC when naive): `older_than:
2025-01-01` means before that instant, `younger_than: 2025-01-01` at or after
it. `older_than` must be the more recent bound when both are the same kind (a
shorter duration, or a later date); a range that can never match fails
validation. Mixed kinds (a duration and a date) are allowed and checked at run
time only.

`arrived_at` is set when a copy is first recorded in a storage location
(register, index add, or an upload when it completes) and again whenever a
copy or move rule transfer makes the destination copy available, including
into a reused `deleted`, `missing` or `unknown` copy row. A move always
resets it, even onto a destination already recorded as available and
identical (whether or not its mtime has changed since), since the file now
lives there because of the move; a copy onto such a destination leaves it
alone. The reset locks the destination copy and bumps its `generation`, so a
rule candidate selected before it (e.g. a scheduled delete of that copy) is
stale and skipped rather than acting during the new cooling-off period. Verify, re-index, accept, overwrite and
metadata or content-date changes leave it alone. So `arrived: {older_than: 1d}` gives a
cooling-off period: a file moved into a location by a one-off rule, or newly
arrived there, is not swept on by a scheduled rule for a day.

`is_latest` is `null` (every version), `true` (latest only) or `false` (all
but the latest).

## Durations

Every duration in config and on the command line is one or more
`<number><unit>` parts, case-insensitive, optionally separated by spaces:
`s`/`sec`/`second(s)`, `m`/`min`/`minute(s)`, `h`/`hr`/`hour(s)`,
`d`/`day(s)`, `w`/`week(s)`, `mo`/`month(s)` (30 days) and `y`/`yr`/`year(s)`
(365 days), e.g. `30d`, `1h30m`, `2h 30m`, `90 days`, `1y6mo`. `m` is minutes.
Months and years are fixed lengths, so a duration is always a plain length of
time. Durations are written back as days, hours, minutes and seconds.

## Selection and watermarks

1. SQL filters live, non-ignored files with an `available`, unowned copy in
   the active source storage location. It also filters `is_latest`, content
   dates, age and the escaped SQL `LIKE` prefix of each include glob.
2. Python applies exact include and exclude globs using the shared matcher.
3. Limits count selected files, not SQL candidates rejected by exact globs.

Every age bound is a SQL condition on its date (see [Age criteria](#age-criteria));
durations are resolved against the run's selection time.

Full mode evaluates the whole index. Incremental mode selects files or source
copies changed since the saved watermark, plus files that crossed any
relative `older_than` bound (on any age field) since the previous selection
window. Absolute bounds and `younger_than` never make a file newly eligible
as time passes. A new or reset `arrived_at` updates the copy, so it is a
change. A latest-flag flip updates
the file timestamp. First incremental runs behave as full.

The change key is `(updated_at, file id)`, using the more recent file/copy
update. Persist both parts, not just the timestamp; tied files beyond a batch
boundary must remain selectable. Age-crossing selection retains its time
window across a chain. Progress moves only past completed work, never past a
failed file. Full and incremental runs use keyset pages, not offsets.
Incremental pages order by the change key. No index can order that
"later of two timestamps" key, so a page is read as two index-ordered
streams, one by `file(updated_at, id)` and one by
`file_storage_location(updated_at, id)`, each seeking past the cursor, and
merged (`models.rules.change_key_page`). A row neither stream returned has a
change key above the last key of a full stream, so only rows up to the
smallest such key are returned: nothing is skipped or reordered, and no page
scans and sorts every source copy. Files that only crossed the age threshold
are a third stream merged the same way. Full pages walk immutable file
ids instead: a rule's own writes cannot move an already-processed file to
the end of its full scan and cause repeated copying. The change watermark
still records the greatest completed file/copy key.

## Runs and batches

`run_rule(project, rule, mode="incremental", dry_run=False, ...)` resolves
the immutable config and current effective enablement. Internal continuation
parameters carry the chain cursor/window; public configured-run inputs are
only `mode` and `dry_run`.
The internal parameter is named `continuation` (Prefect parameters cannot
start with an underscore). A completed batch checkpoints it on its own
Prefect flow run, tagged `hp-rule-checkpoint-p<id>-<rule>-<mode>`, before
saving the DB watermark. A later manual or scheduled run can therefore
resume a paused full/age-selection chain even with `retrigger: false` or
after a process restart. Recovery checks the DB watermark, completed
selection window and config fingerprint; stale continuation parameters do
not rewind progress. A completed chain marks its checkpoint non-pending.
There is no extra hp run table, queue or persisted configuration.

A run holds `hp_concurrency("rule:<project>:<name>")`; each transfer holds
both source/destination storage-location limits in deterministic order.
The deployment uses concurrency 1 and collision strategy `ENQUEUE`: a
continuation that comes due (even with `pause_between_runs: 0`) or a queued
follow-up waits for the running run to finish instead of being cancelled. A
trigger during a running run queues one follow-up keyed to that run, because
the run may already have made its final check; see SVC-24.
Per-file actions are Prefect tasks, using the
[owned operations](operations.md). Counts and individual outcomes go to the
flow-run log, not a Prefect artifact. Every selected file is reported; any
failure fails the run. Dry runs log `would_copy`, `would_move` or
`would_delete` and change neither store nor DB, including progress.

After finishing the current file, stop at the first reached limit:
`batch.max_files` (100), `batch.max_bytes` (`1GB`) or
`batch.max_duration` (`30m`). Rule bytes count source file size. Persist
completed-batch progress. Re-query before deciding whether work remains so
changes arriving during a run are not stranded; the late-change window restarts at the
old window's end timestamp (any id), so a change tied with it is never skipped. With remaining work and
`retrigger: true`, schedule the same deployment after `pause_between_runs`
(`30s`), carrying the cursor. A disabled rule never schedules a successor.
Enablement is checked both before submission and after Prefect creates the
successor. If a disable raced that submission, the successor is cancelled.
File-task failures leave the current batch's checkpoint unadvanced; earlier
completed batches remain resumable, and replay uses the same guarded tasks.

## Deployments, triggers and enablement

Names are `rule-p<project id>-<project>-<rule>`, with module entrypoints and
explicit project/rule parameters. Optional cron comes from YAML; rules without
cron still get deployments. Reject duplicate identities before registration
and remove stale rule deployments/schedules at startup, as for index
deployments. Maintain the rule and storage-location global concurrency limits.

Effective enablement is `config.enabled AND rule_state.enabled` (DB default
true). Disabling first persists the switch, pauses the Prefect schedule and
cancels scheduled future runs. An active batch finishes but cannot retrigger.
Enabling resumes the schedule only when YAML also enables the rule.
Manual submissions and triggers of disabled rules return `409 disabled`.
A manual submission while the rule has an unfinished run is refused with
`409` naming that run (a change trigger is instead a successful no-op).

Trigger checks for scheduled, pending or running deployment runs (paused and
cancelling runs are also treated as unfinished). Future runs that the Prefect
scheduler pre-created for the cron (`auto_scheduled`, expected start still in
the future) are ignored here and by manual submissions: they have selected no
work. Runs hp creates itself (triggers, follow-ups, continuations), due or
overdue scheduled runs and `AwaitingConcurrencySlot` runs still count. If one
exists, return successful `{started: false}`; otherwise submit an incremental
run and return `{started: true, flow_run_id}`. Racing submissions use Prefect
idempotency and a short runtime-state row lock, never an hp queue, to ensure
one run is created, rather than creating duplicate runs and cancelling them.
This includes future cron-created runs: use an on-demand rule (no schedule)
when changes should trigger immediate archiving rather than await that run.
The idempotency key combines the deployment UUID with the next generation
from tagged Prefect run history. This survives API restarts, terminal runs and
an ambiguous submission response. Deployment concurrency remains a second guard.
Deployment availability is checked before taking a write lock, then the
effective switch is rechecked under the lock before submission. Control
requests use short network timeouts without SDK backoff while a runtime-state
lock is held, so a Prefect outage cannot hold up file mutations for an entire
retry cycle.

After committed uploads, registration, overwrite/transfer publication,
source deletion, expired-operation repair, accept, metadata updates,
un-ignore and index batches adding/changing files, collect
enabled rules whose source and match can select affected files. Latest flips
are changes too. Dispatch triggers best-effort outside the change transaction;
log failures and never fail the caller because Prefect is unavailable.
`services.rule_control.notify_file_changes(ctx, project, file_ids)` (defined in
`services.rule_notify`, re-exported) dispatches
the selection and trigger requests without waiting. It reuses the rule
engine's exact selector and conservatively re-evaluates series siblings, so
even earlier latest flips in a multi-version batch are included. It performs no
store I/O. Dispatch preserves the caller's Prefect profile and settings, but
not request-scoped clients that can close before the worker runs. It must not
accidentally start an implicit server in a fresh thread.
The dispatch thread is not a daemon: the caller does not wait for it, but the
process does before it exits, so a flow-run process that publishes a copy and
ends at once still sends its triggers.
Call it once per committed mutation, never inside its transaction.
For destination-only copy publication, pass
`storage_locations={destination}` to restrict selection to rules sourcing the
changed copy, not the unchanged source. This optional keyword defaults to all
sources; an empty collection is a no-op. Leave it unrestricted for logical-file
changes, source deletion, moves or index batches that can change latest flags
across a series; callers need not enumerate those siblings.
After deployment registration, `reconcile_rule_schedules(ctx)` reapplies
persisted switches and cancels future runs of disabled rules; a failure stops
startup rather than reporting a successful reconciliation.

## API, client and UI

`GET /projects/{p}/rules[/{name}]` returns validated config with defaults,
effective state, deployment name and watermark. `deployment_id` and
`deployment_url` identify the actual Prefect deployment by UUID, when it can
be resolved. They are null when Prefect/deployment registration is unavailable
(or, for the URL, the Prefect UI URL is unknown); config and runtime state
remain readable. POST subroutes `enable`,
`disable` and `trigger` change runtime state or submit work; they never edit
YAML. `POST /projects/{p}/runs` supports `kind: rule` with rule name,
mode and dry run, and `kind: rule_once` with a complete `spec`; both return
`202 {flow_run_id, flow_run_url, deployment}`.
Enable/disable returns the resulting rule record. Prefect mutation failures
return `503`, even though the runtime switch has already committed. Retrying
the same action reconciles Prefect; a committed disable already prevents
manual submissions and continuation.

A one-off spec has action, source, optional destination, match and options.
It is not saved and needs no configured rule name or schedule. Invalid specs
return `422`. One shared `rule-once` deployment runs it in full mode for
exactly one batch: limits are totals and retrigger is always off.

`file.ignored_from_all_rules` defaults false and excludes the file from all
rules, never from indexing. `PATCH /files/{id}/rules-ignore {ignored}`
records the user; un-ignore bumps `updated_at`. Records expose the flag and
`find` filters it.

The HTTP-only client exposes `list_rules`, `get_rule`, `run_rule`, `run_once`,
`enable_rule`, `disable_rule`, `trigger_rule` and `set_rules_ignored`.
The latter resolves paths and checks that numeric IDs belong to the bound
project before mutating them. Both `find` and `find_all` accept
`ignored_from_all_rules=None|True|False`.
UI list/detail pages show per-storage-location states and ignored toggles.
The rules page shows config, effective state, schedule, watermark, enable/
disable, run mode/dry run and Prefect links. Its one-off form includes every
rule field. Use the existing protected POST/redirect/GET form pattern.
The rules page is `/projects/{p}/rules`; configured actions post to
`/projects/{p}/rules/{name}/enable`, `/disable` or `/run`, and one-offs to
`/projects/{p}/rules/run-once`. Validation/API errors render an escaped error
page. File toggles post to `/projects/{p}/files/{id}/rules-ignore`.
Deployment links use the returned UUID with the UI's public Prefect URL when
configured, so a separately served UI need not expose the API's internal URL.

## Reconcile (M5)

`reconcile(project, verify=False, rules=None)` (`services/reconcile.py`)
brings a project's index and storage locations fully into line with its
rules in one Prefect flow run. Running it again with nothing changed in
between makes no changes (SVC-06).

### Phases

1. **Index.** `index_full` for every active storage location, in config
   order.
2. **Verify** (only with `verify=True`). Every `available` copy in every
   storage location is re-hashed with the `verify_file` task, in batches,
   holding that storage location's limit. A mismatch makes the copy
   `unknown`, exactly as verify does elsewhere. This finds corruption that a
   full index cannot, because the index trusts an unchanged size and mtime.
3. **Rules.** Every enabled rule (YAML `enabled` and the DB switch) in full
   mode, in config order. `rules` limits this phase to the named rules: an
   unknown name is rejected (`422` at the API), a named disabled rule is
   skipped and logged. Indexing and verify always cover every storage
   location.

### Inline chains

An index or rule run normally stops at its batch limits and schedules a
successor run (R9/R10). Inside reconcile the same batches run **inline**: the
phase calls the batch function with the returned continuation until no work
remains, waiting `pause_between_runs` between batches, and never schedules a
successor. Batch limits still bound each batch and progress is saved after
each batch exactly as in a normal run, so a failed reconcile resumes cheaply
when run again. Nothing new is queued or stored.

### Fixed point

Rules can undo or enable each other's work: a delete rule listed before the
copy rule that feeds it only acts on the next run. The rule phase therefore
repeats whole passes until a pass changes nothing, up to
`reconcile.max_rule_passes` (default 3). If the last allowed pass still
changed something (for example one rule copies what another deletes), the
reconcile fails and names the rules that changed files in that pass.

Changes are counted per phase and per rule: index added, changed, removed,
repaired and swept; verify copies marked `unknown`; rules copied, moved and
deleted files (outcomes `copied`, `moved`, `deleted`), and destinations newly
marked `unknown` by a conflict (outcome `conflict`). Not changes: `skipped`
(newer destination, already conflicted) and `unchanged` (destination already
available and identical). Index changes also include copies newly marked
`missing` or `unknown`. The counts go to the flow-run log; there is no
artifact. Verify pages available copies by id, `reconcile.VERIFY_BATCH` (100)
at a time, holding the storage location's limit for each page.

### Failures

Inside a rule pass a failed file does not stop the pass: it is recorded with
its rule and error, and the pass continues with the remaining files and
rules. When the pass ends the reconcile fails, listing every failure. If
failures reach `reconcile.max_failures` (default 5) the reconcile fails at
once. After any failure no further pass starts, so a fixed point is never
claimed over failed work. Continuing past a failed file never moves a rule's
persisted incremental watermark past it: full mode walks file ids inline.

A failed index or verify batch, or an incomplete repair, fails the reconcile
immediately, naming the phase and storage location (`reconcile index of
<project>/<storage location> failed: <error>`), with the original error
chained. An error that is not a per-file failure (for example from rule
selection) also ends the reconcile at once; the message names the rule and its
error and lists every file failure already recorded in that pass, so none is
lost.

A file that is deleted or flagged `ignored_from_all_rules` after a rule's page
was read is re-checked just before it runs and skipped (logged; neither a
change nor a failure). This applies to every rule run, not only reconcile.

### Concurrency and triggers

One reconcile runs per project: its deployment has concurrency 1 with
`ENQUEUE`, and the flow holds `hp_concurrency("reconcile:<project>")`. Each
index batch holds its storage-location limit and each rule batch its rule
limit, as in normal runs, so scheduled runs and change triggers interleave
with a reconcile instead of overlapping it.

It is started by `POST /projects/{p}/runs {kind: "reconcile", verify?, rules?}`
(`202` with the run link, `409` naming an unfinished reconcile run), the
client's `reconcile()`, "Reconcile now" on the project page, or the optional
cron in project config:

```yaml
reconcile:              # every key optional
  schedule: "0 4 * * 0" # none by default: on demand only
  verify: false         # parameter for scheduled runs
  rules: null           # null = all rules; or a list of rule names
  max_rule_passes: 3
  max_failures: 5
```

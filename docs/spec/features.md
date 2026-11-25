# Features and milestones

This file is the source of truth for **scope**. Each milestone is a tracer
bullet: the thinnest working slice through every component (client → API → DB
→ storage → services → UI → deployment). A milestone is done when its
requirements (see [`requirements/`](requirements)) all have passing tests, the
coverage gate (90%) holds, and the end-to-end checks below pass.

Status key: **planned**, **in review**, **building**, **paused**, **done**.

| Milestone | Title | Status |
|---|---|---|
| M1 | Walking skeleton | done |
| M2 | Versions and queries | done |
| M3 | Indexing (existing projects and external changes) | done |
| M4 | Multiple storage locations and archiving rules | done |
| M5 | Full reconciliation | done |
| M5A | Auth | done |
| M6 | UI redesign and cross-project search | done |
| M6A | Number and text versions; rule ages; alpha hardening | done (alpha-readiness additions in PR) |
| M7 | Storage locations: SFTP, credentials and rclone | in review |
| M8 | File analysis and pipelines | planned |
| M9 | Post-ingest jobs | planned |

---

## M1: Walking skeleton

**Goal:** one file goes from a client into a local store, is verified, and
shows in the UI, with everything started by one command.

Requirements: CLI-01, CLI-04, API-01, API-03, API-05, API-06, DB-01, DB-03,
DB-04, STO-01, SVC-01, SVC-02, UI-01, UI-03, UI-05, DEP-01, DEP-02, DEP-05.

In scope:

- `heliopuddle server start [--host] [--port] [--no-api] [--no-ui]
  [--no-services] [--no-migrate]` runs in one process:
  - DB migrations (unless `--no-migrate`; then the schema must already be at
    head or startup fails);
  - the API (`/api/v1`) and the UI (`/`) on one uvicorn server, or the UI on
    its own port when `HELIOPUDDLE_UI_PORT` differs from the API port;
  - the services: Prefect deployments served with `prefect.serve()`;
  - an embedded Prefect server as a child process when `PREFECT_API_URL` is
    unset. When it is set, startup checks its `/health` and fails fast if it is
    unreachable.
- `heliopuddle db upgrade` runs the migrations only (no API, UI or services)
  and exits 0 on success.
- The UI can also run as a separate process (`--no-api --no-services`) against
  `HELIOPUDDLE_UI_API_URL`; `HELIOPUDDLE_UI_ROOT_PATH` and
  `HELIOPUDDLE_API_ROOT_PATH` support reverse proxies.
- The same `server start` command is the Docker image's entrypoint.
- Settings from `HELIOPUDDLE_*` env vars (pydantic-settings):
  `CONFIG_DIR`, `DATA_DIR`, `DATABASE_URL` (default SQLite under `DATA_DIR`),
  `HOST`, `PORT`, `USER_HEADER` (default `X-Forwarded-User`),
  `PREFECT_UI_URL`, `UI_HOST`, `UI_PORT`, `UI_API_URL`, `UI_ROOT_PATH`,
  `API_ROOT_PATH`.
- Project config: one YAML file per project in `CONFIG_DIR`. M1 supports one
  project with one fsspec storage location (tested with `file://`).
  `${VAR}` / `${VAR:-default}` in `url` and `storage_options` values are
  resolved from the environment at load time. Invalid config fails
  startup.
- Database: SQLite, schema created by alembic migrations. Tables `project`,
  `storage_location`, `series`, `file`, `file_storage_location` (see
  [data model](../design/data-model.md)). Projects and storage locations are
  synced from config into the DB on start.
- In M1 every file is **unversioned**: each path is its own series and is
  always `is_latest`. Version rules arrive in M2.
- API:
  - `GET /health`, `GET /version`;
  - `GET /projects`, `GET /projects/{p}`;
  - `GET /projects/{p}/storage-locations`,
    `GET /projects/{p}/storage-locations/{name}` (URL and resolved storage
    options);
  - `POST /projects/{p}/files` (register) → `201` with the file record, already
    verified (`available` or `unknown`);
  - `GET /projects/{p}/files` (list, simple paging), `GET /files/{id}`.
  - The user id comes from the configured header and is stored as
    `created_by`. A missing header is allowed (`created_by` is NULL).
- Client `HelioPuddleClient(api_url, project, user=None)`:
  `add_file(local_path, path, storage_location=None, metadata=None)` fetches
  the storage-location config, hashes the file (md5), writes it with fsspec
  (via a `.hp-tmp` name and rename on filesystems that support it), registers
  it, and returns a `FileRecord`. Also `get_file(id)` and `list_files()`.
  `storage_location=None` means the project's first storage location.
- Services: the Prefect **task** `verify_file(file_storage_location_id)`
  re-hashes the copy in the store and sets `available` on a match, `unknown`
  on a mismatch or a missing file. The API calls it **inline** during
  register. The **flow** `verify_files(file_storage_location_ids)` runs the
  same task over many files and is a served deployment for background and
  bulk re-verification.
- UI: a file list page for the project (path, size, state, created, created
  by) with a link to the Prefect UI.
- Docker image (non-root, `tini`) with volumes `/data`, `/config`,
  `/var/lib/heliopuddle`.
- Tooling: uv project, ruff, pyright, import-linter (client and UI may not
  import the DB layer), pytest with the `req` marker, `scripts/req_matrix.py`,
  coverage gate 90%, GitHub Actions CI (lint, import contracts, unit and
  integration suites, Python 3.11–3.14).

Out of scope for M1: version rules, re-add no-op, conflicts and overwrite,
remove, queries beyond list/get, Postgres, indexing, multiple storage
locations, rules.

End-to-end checks:

1. `uv run heliopuddle db upgrade` on a fresh DB exits 0.
2. `uv run heliopuddle server start` with `examples/projects/demo.yaml`.
3. `HelioPuddleClient(...).add_file(...)` → the file is in the store and the
   returned record is `available`.
4. The UI file list shows the file (Playwright screenshot), with the UI on the
   shared port and on its own port.
5. The Prefect UI shows the inline `verify_file` task run, and a submitted
   `verify_files` deployment run completes.
6. The same with `docker run` and a `/data` volume; `/api/v1/health` is OK.

## M2: Versions and queries

**Goal:** files have server-derived versions and series with a correct
`is_latest`; re-adds are idempotent, conflicts are explicit, files can be
removed and queried; Postgres and an external Prefect server work.

Requirements: API-02, API-07, API-08, API-09, CLI-02, CLI-03, CLI-05, CLI-06,
DB-01b, DB-02, SVC-07, UI-02, UI-06, DEP-04, DEP-06 (plus every M1
requirement). Approved plan: [reviews/m2-plan.md](reviews/m2-plan.md).

In scope:

- Version rules per path in the project YAML (`versioning:`), first match
  wins, with the defaults `v<major>.<minor>.<ext>` and `v<version>.<ext>`
  (1–10 digits) appended; `latest_by: major_minor | minor`. Series keys keep
  single and major/minor versions apart. Rules are validated at startup. See
  [versioning](../design/versioning.md). (Replaced by number and text versions
  in M6A.)
- `is_latest` and `series.latest_file_id` recomputed in the add, overwrite and
  remove transaction under the series row lock.
- Optional `content_date` on register, else from a `date` group in the rule.
- `POST /projects/{p}/files/check` and `GET /projects/{p}/files/by-path`.
  Re-adding an identical, `available` file is a no-op with no transfer.
- A different hash at a live path returns `409` unless `overwrite=true`; the
  client checks first and raises `ConflictError` without touching the store.
- Remove: `DELETE /files/{id}` calls the `delete_file` task inline; the
  `delete_files` flow is served as the deployment `delete_files/delete-files`.
  Deleted rows stay as history; re-adding a deleted path creates a new row.
- `PATCH /files/{id}/metadata` (merge, or replace with `?replace=true`).
- `updated_by` column (migration `0002`).
- Queries: list filters (path glob, series, `latest_only`, state, storage
  location, content date range, `include_deleted`, paging, order);
  `GET /series/{id}/versions`. Client `find`, `find_all`, `list_versions`,
  `latest`, `remove_file`, `open`, `download`.
- UI: file detail page with version history, per-storage-location state and
  metadata; filters on the file list.
- Postgres alongside SQLite (migrations, row locks). SQLite reads no longer
  take the write lock; only writes use `BEGIN IMMEDIATE`.
- Postgres plus an external Prefect server configured by env vars alone.

Out of scope for M2: indexing, multiple storage locations in practice, rules,
any API to change config.

End-to-end checks:

1. Add `x_v001.csv` then `x_v002.csv`: `latest` is v002; `list_versions`
   shows both.
2. Re-add the same v002: no transfer. Add different content as v002:
   `ConflictError`; with `overwrite=True` it replaces it.
3. `remove_file` v002: v001 becomes latest.
4. The UI detail page shows the history (screenshot); a `delete-files`
   deployment run completes in Prefect.
5. The same against Postgres and an external Prefect container, configured by
   env vars only; and the Docker e2e suite.

## M3: Indexing (existing projects and external changes)

**Goal:** the DB follows what is actually in each storage location: existing
folders and buckets are adopted, and files added, changed or removed directly
in the store are picked up by scheduled and on-demand index flows. Azure Blob
works (tested against Azurite) and runs in the Docker image.

Requirements: SVC-03, SVC-08, SVC-09, SVC-10, API-10, API-11, API-12, CLI-07,
STO-02, STO-06, STO-07, STO-08, STO-09, STO-10, STO-11, STO-12, STO-13,
SVC-11 to SVC-18, API-13, CLI-08, UI-07, DEP-03, DEP-07, DEP-08, DEP-09, DEP-10,
DEP-11, DEP-12, DB-05 (plus every M1 and M2 requirement).
Approved plan: [reviews/m3-plan.md](reviews/m3-plan.md).

In scope:

- `index_full(project, storage_location)` and `index_incremental(project,
  storage_location, since=None)` flows; listed entries are processed by an
  `index_batch` task per `index.batch_size` entries (default 1).
- Diff rules for `accept_external_changes` on and off (see
  [indexing](../design/indexing.md)); unknown files are recorded but never
  latest; any `unknown` copy can be accepted by an operator.
- Latest eligibility: only live files with a `pending` or `available` copy can
  be latest.
- Repair of stale `pending` (re-verify) and `deleting` (retry) copies.
- Deployments per storage location with cron schedules from the project
  `index` config; adoption by a full index on first start; concurrency per
  storage location (full waits, incremental skips).
- `POST /projects/{p}/runs {kind: index}`; `POST /files/{id}/accept`; index
  times on storage locations. Client `trigger_index`, `accept_file`.
- UI: storage-locations panel with "Index now"; "Accept as is" on unknown
  copies.
- Storage generic over fsspec: no backend-specific code outside
  `storage/backends.py`; local, SMB, SFTP, S3, Azure Blob and GCS supported out
  of the box via `heliopuddle[all]` extras; per-storage-location `create_root` (local roots created automatically, buckets/containers
  not by default).
- Easy setup: `server start` with no config writes `<data dir>/default_config.yaml` (a `default` project) and loads it. Azure (Azurite) is the canonical cloud backend
  for the slow tests, with one conformance suite for all test backends. See
  [storage](../design/storage.md).
- Dependencies current, with `scripts/check_deps.py` and a per-milestone
  upgrade rule.

Out of scope: copy/move/delete rules and multiple storage locations working
together (M4); reconcile (M5); moving existing files to a new series when the
version rules change.

End-to-end checks:

1. A local tree of versioned files is adopted on first start; the UI shows
   every file with the right latest.
2. A file dropped, changed and deleted directly in the store is picked up
   (incremental for new/changed, full for removals).
3. With `accept_external_changes: false` a dropped file shows as `unknown`
   and not latest; "Accept as is" makes it `available` and latest.
4. "Index now" from the UI starts a run visible in Prefect.
5. The same with an Azurite storage location in the Docker image.

## M4: Multiple storage locations and archiving rules

**Goal:** safely archive files between a project's storage locations through
configured or one-off rules, with bounded, resumable Prefect runs.

Requirements: SVC-04, SVC-05, SVC-19 through SVC-25, STO-03, STO-04, STO-14,
API-04, API-14, API-15, CLI-09 (including the CLI-08 retry amendment), UI-04,
DEP-13 and DB-06, plus every earlier milestone requirement.
Approved implementation plan: [reviews/m4-plan.md](reviews/m4-plan.md).

M4 was tested by the owner and merged to `main` (PR #4).

M3 was merged before M4 began. The owner's instruction to build that plan
approves its decisions R1-R19:

| Decision | Scope |
|---|---|
| R1 | Rule delete affects only the source copy; soft-delete the file when no live copies remain. |
| R2 | A transfer snapshots the source generation and hash, without holding its ownership token. A move deletes only that unchanged source revision, after destination verification. |
| R3 | Destination writes hold a renewable token; absent objects are written directly, existing objects are staged and published. Use generic `fs.copy` when `backends.py` confirms the same filesystem, otherwise stream. Full indexing repairs expired transfers. |
| R4 | Same destination hash: verify and mark available without writing. Different hash: skip a newer destination when requested; otherwise replace only with `overwrite`; otherwise mark unknown and log the skip. |
| R5 | Free space is native on local, SMB and SFTP; optional storage-location `quota` works everywhere using indexed size. Take the smaller native/quota value. Object stores without quotas are unlimited. Default space checking is on with `2GB` overhead. |
| R6 | Modified age uses source-copy mtime, falling back to file creation. Content-date age never matches a file without a content date. |
| R7 | Latest-flag changes bump each affected file's `updated_at`. |
| R8 | Incremental rules select changed files/copies and age crossings, using `(updated_at, id)` keysets. First incremental runs behave as full. |
| R9 | Rule and index runs stop after the current file at the first files/bytes/duration limit: defaults 100, `1GB`, `30m`. Index bytes count only content read for hashing. Persist progress after each batch; retain a full-index chain's start until final removal detection. |
| R10 | Re-query for remaining work and schedule the same deployment after `pause_between_runs` (default `30s`). `retrigger` defaults true; disabled rules do not retrigger. |
| R11 | All rule options have defaults. `batch.max_files` replaces `max_files`; a minimal rule needs name, action, source, destination except for delete, and match paths. |
| R12 | Unsaved one-off specs are validated like config and run through `rule-once` in full mode for one batch, with limits as totals and no retrigger. Configured runs accept only mode and dry run, never config overrides. |
| R13 | Both YAML `enabled` and `rule_state.enabled` must allow a run. Disable pauses schedules and cancels future runs; an active batch finishes without retrigger. Enable resumes schedules; disabled manual runs/triggers return `409`. |
| R14 | `ignored_from_all_rules` defaults false, excludes a file from every rule, and is editable through API/client/UI. Un-ignore updates the timestamp; indexing still tracks ignored files. |
| R15 | Committed file changes best-effort trigger matching enabled rules. A scheduled or pending run makes trigger a successful no-op. While only a run is running, trigger queues exactly one follow-up keyed to that run (it may have made its last check already). Deployment concurrency 1 with `ENQUEUE`: continuations and follow-ups wait for the slot rather than being cancelled. Manual runs are refused with `409` while any run is unfinished. (Owner review amendment; was `CANCEL_NEW` with a no-op during a running run.) M5-review amendment: future runs the Prefect scheduler pre-created for a cron are not "unfinished", so they neither refuse manual runs nor make triggers no-ops. |
| R16 | Rule deployments are `rule-p<pid>-<project>-<rule>`, cron or on-demand; reject duplicates and remove stale deployments. Hold the rule limit and both transfer storage-location limits. |
| R17 | Rule counts and per-file outcomes go to flow-run logs, not artifacts; any file failure fails the run. Dry run logs `would_*` and changes nothing. |
| R18 | Fail startup for invalid names/references, action/destination combinations, globs, durations, sizes, dates, cron or non-positive batch limits. |
| R19 | Retry upload completion a bounded number of times on `503` with `Retry-After`. |

End-to-end checks:

1. A two-location project (local primary, Azurite archive with quota) moves
   an old, non-latest version only after verifying the archive copy.
2. 250 files sharing a timestamp traverse three 100-file batches without
   skips (live acceptance passed on `03d3393`; fast unit regressions remain);
   full-index chains detect removals only at completion.
3. An interrupted transfer leaves its source intact and is repaired by the
   full index. Fault/interleaving tests extend the M3 ownership invariants.
4. Disable/enable pauses/resumes schedules; ignored files stay put.
5. One-off dry and real runs, source-scoped delete rules, client methods,
   rules UI and Prefect deployments/runs work end to end.
6. SQLite/Postgres migrations, unit coverage at least 90%, all
   integration/Docker e2e suites and the complete M4 requirement matrix pass.

Out of scope: reconciliation (M5), mirror/sync targets (M7), analysis,
post-ingest jobs (M8-M9); auth is M5A.

See [rules](../design/rules.md) and [operations](../design/operations.md).

## M5: Full reconciliation

**Goal:** one run that brings a project's index and storage locations fully
into line with its rules, and makes no changes when run again.

- `reconcile(project, verify=False, rules=None)` flow, on demand or on an
  optional schedule: `index_full` for every storage location, an optional
  deep verify, then the enabled rules in full mode until nothing changes.
  Idempotent.
- `POST /projects/{p}/runs {kind: reconcile, verify?, rules?}`, the client's
  `reconcile()` and "Reconcile now" in the UI.

Requirements: SVC-06, SVC-26, SVC-27, SVC-28, API-04 (amended), CLI-10, UI-08
and DEP-14, plus every earlier milestone requirement.
Approved implementation plan: [reviews/m5-plan.md](reviews/m5-plan.md).

| Decision | Scope |
|---|---|
| C1 | One flow run. Each phase loops its index or rule batches inline with their continuation, instead of scheduling successor runs. Batch limits bound each batch, progress is saved per batch, and `pause_between_runs` separates batches. No new queues or tables. |
| C2 | Phases: (1) `index_full` for every active storage location in config order; (2) optional verify (C4); (3) every enabled rule (YAML and DB switch) in full mode, in config order, ignored files excluded. An optional `rules` list limits phase 3 to those rules: an unknown name is `422`, a named disabled rule is skipped and logged. Phases 1 and 2 always cover every storage location. |
| C3 | The rule phase repeats until a whole pass changes nothing, up to `reconcile.max_rule_passes` (default 3). Still changing after the last pass fails the run, naming the rules that changed things. |
| C4 | `verify=true` re-hashes every `available` copy with the `verify_file` task, batched, holding the storage-location limit; a mismatch becomes `unknown`. Off by default. |
| C5 | Changes: index added/changed/removed/repaired/swept; verify copies marked `unknown`; rules copied/moved/deleted and newly conflicted (`unknown`) destinations. Skips are not changes. Per-phase and per-rule counts go to the flow-run log; no artifact. |
| C6 | One reconcile per project: deployment concurrency 1 with `ENQUEUE`, plus `hp_concurrency("reconcile:<p>")`. Index and rule batches hold their existing storage-location and rule limits, so scheduled runs and triggers interleave safely. |
| C7 | A failed file does not interrupt a rule pass: it is recorded and the pass continues with the remaining files and rules, then the reconcile fails listing every failure. Reaching `reconcile.max_failures` (default 5) fails at once. After any failure no further pass starts. A failed index or verify batch, or a failed repair, fails the reconcile immediately, naming the phase and storage location. |
| C8 | Started by `POST /projects/{p}/runs {kind: "reconcile", verify?, rules?}` (`202` with the run link, `409` naming an unfinished reconcile), the client's `reconcile()`, a "Reconcile now" form on the project page, or an optional cron. |
| C9 | Project YAML `reconcile: {schedule, verify, rules, max_rule_passes, max_failures}`, every key optional (defaults: none, false, all rules, 3, 5). `verify` and `rules` are the scheduled runs' parameters. Deployment `reconcile/reconcile-p<pid>-<project>`, on-demand without a schedule; stale ones are removed on start. |

End-to-end checks:

1. A two-location project whose copy and delete rules are listed in an order
   that needs two passes reaches a fixed point in one reconcile; a second
   reconcile reports zero changes and leaves store and DB unchanged.
2. A file corrupted without changing its size or mtime is found only by
   `verify=true`.
3. A locked file fails its pass at the end while the other files and rules
   complete; a concurrent reconcile submission returns `409`.
4. "Reconcile now" in the UI submits a run and links to it in Prefect.

## M5A: Auth

**Goal:** every API and UI request is authenticated and authorized by realm
roles. hp works on its own with users and passwords (OAuth2 password flow) and
with an external Keycloak (full OAuth). It is built between M5 and M6, so M5
and M6–M8 keep their numbers. (M6–M8 later moved to M7–M9 when the UI
redesign became M6.) Requirements: AUTH-01 to AUTH-17
([auth](requirements/auth.md)). Approved plan:
[reviews/m5a-plan.md](reviews/m5a-plan.md). Design: [auth](../design/auth.md).

In scope:

- `HELIOPUDDLE_AUTH_MODE`: `local` (default), `oidc` or `header` (the
  trusted-proxy behaviour before M5A). **Upgrade note:** deployments behind a
  proxy that forwards `HELIOPUDDLE_USER_HEADER` must set
  `HELIOPUDDLE_AUTH_MODE=header`.
- `local`: users in the DB, `POST /auth/token` (password flow), HS256 tokens,
  a first-start admin (generated password logged once, or from env vars), user
  CRUD API and a `heliopuddle user` CLI that calls only the API.
- `oidc`: bearer JWTs validated against the issuer's JWKS, optional
  introspection, client id and secret from env vars.
- Roles `hp-reader`, `hp-writer`, `hp-deleter`, `hp-operator`,
  `hp-client-storage-read-write`, `hp-admin`;
  all but `hp-admin` can be scoped to a project as `<role>:<project>`.
- Client credentials (token, username/password, client id/secret); UI login
  (form in `local`, authorization code with PKCE in `oidc`).
- Backend connection details require `hp-client-storage-read-write` (global
  or project-scoped), or `hp-admin`, in `local` and `oidc` modes. Clients
  without that role transfer bytes through reader/writer-protected API routes.
- `examples/keycloak/` and a manual test.

Out of scope: client roles (`resource_access`), login lockout, refresh tokens,
authenticating the Prefect server and UI, token revocation lists, a UI for
user management, project-scoped `hp-admin`.

End-to-end checks:

1. Fresh `server start`: the log shows the generated admin password; a client
   with that username and password lists projects; no credentials gets `401`.
2. `heliopuddle user add` creates a reader scoped to one project; that user
   sees only that project and gets `403` on a write.
3. The UI login form works; write controls are hidden without the role.
4. With `HELIOPUDDLE_AUTH_MODE=header` the previous behaviour holds.
5. Manual: the same against `examples/keycloak` in `oidc` mode (AUTH-17).

## M6: UI redesign and cross-project search

**Goal:** the UI becomes one coherent app for several projects, each with
typically 1–5 storage locations (more must still work) and tens of thousands
of files, in the approved "Observatory" design; a search API finds files
across every project the caller may read.
Requirements: API-09 (amended), API-16, API-17, API-18, CLI-11, CLI-12,
UI-01, UI-02, UI-04, UI-06, UI-07, UI-08 (amended), UI-09 to UI-15, DEP-15,
DEP-16, CLI-13, CLI-14, CLI-15, CLI-16, API-19, DEP-09 (amended), DEP-17, DEP-18, plus every earlier milestone
requirement. Approved plan: [reviews/m6-plan.md](reviews/m6-plan.md). Design:
[ui](../design/ui.md).

In scope:

- `GET /search/files`: case-insensitive text search over file paths across
  the caller's readable projects, ranked, with per-project counts; the
  client's `search()`.
- `GET /projects/{p}/summary`: per-storage-location copy-state counts and
  bytes, and view counts; the client's `project_summary()`.
- `GET /projects/{p}/files` accepts several `state` values (any of) and
  `deleted_only`.
- `GET /files/{id}/preview`: a bounded preview of CSV, TSV, JSON, JSON lines,
  text, Markdown, image, Parquet and CDF files (CDF read with `cdflib` and
  grouped into tables as `crump` does), shown on the file page; the client's
  `preview()`. New setting `HELIOPUDDLE_PREVIEW_MAX_BYTES` (default 1GB).
- A published Docker image on Docker Hub: configured only by environment
  variables (SQLite, embedded Prefect and local users by default; external
  Postgres, Prefect and OIDC by env), compose examples, and a workflow that
  publishes release tags, `main`, and `dev-*` tags for manual builds.
- A new UI shell (starfield header, project switcher, project tabs, search
  box, user menu) and pages: projects home, project Overview, Files with an
  inspector, Storage, Rules (with a data-flow diagram), file page and search
  results. Light theme, bundled fonts, phone-width layouts.
- The basics of every page work without JavaScript (content, links, GET
  filters and paging, POST action forms); some features need it (typeahead
  search, the in-page inspector, column chooser).

- Embedding the client in another application: the core install is the
  client only and the server moves to the `server` extra (DEP-17), and the
  client's `register_file()` registers a file another tool has already written
  to the store (CLI-13).
- The client's dependencies have old floors (`pydantic>=2.5`, `httpx>=0.24`,
  `fsspec>=2023.1`, `typer>=0.13` for the CLI, ...) tested in CI at exactly
  those versions, so it installs next to other projects' pins (DEP-18).
- A test harness for those applications: `heliopuddle.testing`'s in-memory
  `FakeHelioPuddleClient` and the `heliopuddle_fake` pytest fixture, kept
  faithful by a contract test against the real API (CLI-14).
- `heliopuddle file add|register|list|show|update|download|remove` for
  managing files from the command line, choosing them by id or with
  `--path`/`--name` globs, installable with `heliopuddle[cli]` (CLI-15).
- Changing a file's content date after it is added:
  `PATCH /files/{id}/content-date` and the client's `set_content_date()`
  (API-19, CLI-16); `heliopuddle file update` sets metadata and the content
  date.

Out of scope: a runs/activity API (the UI links to Prefect), a metadata
filter, a "% replicated" figure, a search index beyond SQL `LIKE`, a UI for
user management.

End-to-end checks:

1. With three projects and a user scoped to two of them, the home page shows
   only those two; header search and `GET /search/files` find files in both
   and never in the third.
2. The Overview heatmap cell for "missing on archive" opens Files filtered
   to exactly those files.
3. Files: switching views, adding and removing filters, paging and opening
   the inspector work; with JavaScript off the list, filters, paging and the
   file page still work.
4. Index, Reconcile, Accept as is, Ignore from rules, rule enable/disable
   and Run now still submit and link to Prefect.
5. Playwright screenshots of every page at desktop and phone widths;
   search over 200k files on Postgres answers within 500 ms.

## M6A: Number and text versions; rule ages

**Goal:** a version is either an integer or a normalised string, extracted
from the file name by a regex and, for text, a format template; clients may
give the version themselves when they create a file. Major/minor versions and
`latest_by` are removed (breaking: there are no deployments to migrate).
Rules select files by several dates at once (modified, created, content date
and arrival in the source location), each with older-than/younger-than
bounds, so a moved file gets a cooling-off period before rules touch it
again; the old `older_than`/`age_by`/content-date bounds are replaced.
Requirements: API-02 (amended), API-19 (amended), API-20, API-21, API-22,
DB-07, DEP-06 (amended), DEP-12 (amended), CLI-02 (amended), CLI-13 (amended),
CLI-14 (amended), CLI-15 (amended), SVC-04 (amended), SVC-05 (amended),
SVC-29, SVC-30, DB-08, DEP-13 (amended), DEP-19, API-06 (amended), AUTH-18, AUTH-19,
AUTH-20, STO-15, STO-16, API-23, DEP-20, UI-04 (amended), DEP-16 (amended), DEP-09 (amended),
plus every earlier milestone requirement.
Approved plan: [reviews/m6a-plan.md](reviews/m6a-plan.md). Design:
[versioning](../design/versioning.md).

In scope:

- Rule fields `paths`, `version_type` (`number` | `text`),
  `extraction_pattern`, `version_text` (a Python format template over the
  named groups) and `ignore_in_series` (groups that do not split a series,
  e.g. `ext`). `pattern` and `latest_by` are removed.
- One default rule, `_[vV](?P<version>\d{1,5})\.[^./]+$`, used only when a
  project sets no `versioning`.
- `file.version_number` / `file.version_text` (at most one; indexed per
  series) and `series.version_type`; a series never mixes kinds.
- Version overrides on register and reserve, `add_file` / `register_file`,
  the fake client and `heliopuddle file add|register`; versions are fixed at
  creation.
- `match.age` with `modified`, `created`, `content_date` and `arrived`, each
  `older_than`/`younger_than` (a duration or an ISO date); `is_latest`
  unchanged. `file_storage_location.arrived_at` is set on
  first arrival and on every copy/move rule transfer into the location.
- Durations accept `s`, `m`, `h`, `d`, `w`, `mo` (30d) and `y` (365d) in any
  combination, short or long form.
- The one-off run form and rule descriptions show every age criterion.
- The Docker image is always published to the GitHub container registry
  (`ghcr.io/<owner>/<repo>`); Docker Hub is an opt-in extra target
  (`DOCKERHUB_REPOSITORY`). Pull requests get `pr-<number>` images. The docs
  and compose examples use the Docker Hub image `heliopuddle/heliopuddle`.
- Flow and task runs log their significant changes (transfers, deletes,
  verifies, uploads, index summaries) through Prefect's run logger at INFO, so
  they show in the Prefect UI (SVC-30); the docs say how to set Prefect logging
  when hosting your own Prefect server.
- Security hardening: the Prefect port is published on loopback only (DEP-20);
  `/health` returns no exception text (API-06); login throttling (AUTH-18);
  security headers (AUTH-19); `header` mode refuses a request without the user
  header (AUTH-20); local storage never follows symlinks (STO-15); storage URL
  passwords are redacted (STO-16); `max_upload_size` (API-23).
- CI no longer runs `scripts/check_deps.py`: a release after the last
  `uv lock --upgrade` must not fail an unrelated build; it is run by hand.

Out of scope: changing a file's version after creation, moving files to a new
series after a rule change, natural (numeric-aware) ordering of text,
calendar-exact months and years, a filesystem birth time for `created`.

End-to-end checks:

1. `server start` with no config; `file add` of `x_v3.csv` then `x_v12.csv`:
   `x_v12.csv` is latest.
2. `file add y.csv --version-text 001.0002` stores a text version;
   `--version-number` with `--version-text` exits 2.
3. A project with the `{major:0>3}.{minor:0>4}` text rule indexes
   `a_v1.2.csv` and `a_v1.10.csv`: `a_v1.10.csv` (`001.0010`) is latest, on
   SQLite and Postgres.
4. The UI file page and version history show the versions.
5. A one-off move from `primary` to `archive` followed by a run of a scheduled
   `archive` to `primary` rule with `arrived: {older_than: 1d}` leaves the
   file in `archive`; the one-off form accepts every age field.

## M7: Storage locations: SFTP, credentials and rclone

**Goal:** SFTP is proven as an fsspec storage location; storage credentials
can come from the config, the environment or Prefect blocks; and rclone
remotes (SharePoint, OneDrive, Box, ...) are storage locations through a new
`rclone://` fsspec backend. Mirror rules are dropped: the existing copy, move
and delete rules already sync a subset of files, including deletes.
Requirements: STO-05 (rewritten), STO-17, STO-18, STO-19, STO-20, STO-21,
STO-22, STO-23, DEP-08 (amended), DEP-21, DEP-22, plus every earlier milestone
requirement.
Approved plan: [reviews/m7-plan.md](reviews/m7-plan.md). Design:
[storage](../design/storage.md#credentials).

In scope:

- SFTP through fsspec only (no paramiko-specific code), tested against a real
  SFTP server container.
- Credentials inline, from `${VAR}` or a backend's own environment variables,
  from Prefect Secret blocks (`{{ prefect.blocks.secret.<name> }}`) or from a
  Prefect `RemoteFileSystem` block (`block: <name>`). Blocks resolve lazily; a
  missing block marks the storage location `incomplete` (API `status` and
  `problems`, UI warning) and never stops startup.
- `rclone://<remote>/<path>` storage locations over a local `rclone rcd`:
  config in a prefect-managedfiletransfer `RCloneConfigFileBlock` (or a
  file) with refreshed tokens saved back; one rcd per config across the
  cluster (Prefect global concurrency limit `rclone:<config>`); rcd on a
  private Unix socket with per-session credentials; native-hash verify
  (`verify: auto | native | content | size`); the binary bundled in the image
  and otherwise downloaded at a pinned version with pinned hashes.
- An rclone storage location is never a project's first storage location
  and is never accessed directly by clients.
- A storage location can hold one project folder (`path_prefix`, STO-23),
  e.g. an rclone mirror of `upload_mirror/`; the mapping is in
  `StorageLocation`, so every backend supports it.

Out of scope: rclone as the first storage location (see
[later](requirements/later.md)), an rcd service shared over the network,
Prefect integration credential blocks (prefect-aws, prefect-azure,
prefect-gcp), real Box or SharePoint in CI.

End-to-end checks:

1. The conformance suite passes on SFTP (container) and on rclone over WebDAV
   (container).
2. `server start` with an `rclone://` location whose config block does not
   exist starts; the UI shows the warning; creating the block makes the
   storage location usable without a restart.
3. A copy rule from the first storage location to an rclone location and back
   verifies by native hash.
4. A refreshed OAuth token (a fake local token endpoint) is saved to the block.
5. Two service processes never run an rcd on the same config at once.
6. `server start` with no rclone on PATH downloads the pinned binary.

## M8: File analysis and pipelines

- A ported and improved pipeline stage framework (`heliopuddle.pipelines`).
- Analyser plugins plus a built-in, config-driven time-series analyser.
- Results stored in `file_analysis` and shown in the API, client and UI.

## M9: Post-ingest jobs

- CSV/CDF → Postgres table ingest, table retention (delete old rows), and
  flow triggers after other flows complete.

---

## Milestone process (applies to every milestone until release 1.0)

1. **Spec review.** The next milestone's spec is reviewed with the project
   owner as a plan that includes a table of every in-scope requirement with
   its full description.
2. **Docs first.** Once the plan is approved, `docs/spec` and `docs/design`
   are updated to match and committed.
3. **Build** test-first, delegated to agents with narrow briefs that cite the
   REQ IDs they own.
4. **Verify** end to end: unit and integration suites, coverage ≥ 90%, a
   complete REQ matrix, `docker run`, `uv run heliopuddle server start`, a
   client add, the UI, and the Prefect runs.
5. **Pull request.** Push and open a PR to `main`; the owner tests it.
6. **Gate.** The next milestone does not start until that PR is merged to
   `main`. Work then restarts from the latest `main`.

## M1 review decisions

| Topic | Decision |
|---|---|
| Client binding | `HelioPuddleClient(api_url, project, user=None)`: one project per client |
| Missing user header | Accepted; `created_by` is NULL (until M5A) |
| Hash algorithm | md5, fixed for now; stored in `hash_algo` |
| Verification | One-file verify is a Prefect task, called inline by the API during register; the `verify_files` flow runs the same task over many files as a deployment |
| Config sync | A storage location removed from YAML is marked inactive, never deleted |
| Migrations | Run by default on `server start`; `--no-migrate` skips them; `heliopuddle db upgrade` only migrates and exits |
| Client/DB boundary | Enforced in CI with import-linter |
| Naming | "storage location" everywhere: URLs `storage-locations`, YAML `storage_locations`, tables `storage_location` and `file_storage_location` |
| Ports | API and UI share a port by default; the UI can have its own port or run as a separate process (UI-03 moved from M2 to M1) |

## M2 review decisions

| # | Topic | Decision |
|---|---|---|
| D1 | Remove | `delete_file` task called inline by `DELETE /files/{id}`; `delete_files` flow served as a deployment for bulk use. A failed store delete leaves the copy `deleting` |
| D2 | Series keys | Single versions → `{v}`; major/minor → `{major}.{minor}`; unversioned → the path. They never share a series |
| D3 | Idempotent re-add | `check` returns `{exists, hash_matches, file}`; the client skips the transfer only when the hash matches and the copy is `available`. Registering the same hash returns 200 with the existing record |
| D4 | Conflict | Different hash at a live path → `409` unless `overwrite=true`; overwrite updates the same row and re-verifies. The client checks before writing |
| D5 | Deleted paths | Re-adding a deleted path creates a new row; deleted rows stay as history |
| D6 | Content date | From the register request, else a `date` group in the version rule |
| D7 | Routes | Project-scoped `files/by-path` and `files/check`; id-scoped delete, metadata and series versions |
| D8 | SQLite locking | Reads are deferred transactions; writes use `BEGIN IMMEDIATE`. Postgres locks the series row with `FOR UPDATE` |
| D9 | Metadata | Shallow merge by default, `?replace=true` replaces, `null` removes a key |
| D10 | `updated_by` | New column from the user header on overwrite, metadata change and delete |

## M3 review decisions

| # | Topic | Decision |
|---|---|---|
| D1 | Task granularity | Index flows run one `index_batch` task per `index.batch_size` listed entries (project setting, default 1) |
| D2 | Unknown files | In a location that does not accept external changes, a never-seen file gets a file row and an `unknown` copy |
| D3 | Latest eligibility | Only live files with at least one `pending` or `available` copy can be latest |
| D4 | Accept | Any `unknown` copy can be accepted as is: re-hashed, file hash/size updated, copy `available` |
| D5 | Settle time | Files modified within `index.settle_time` (60s) are skipped until the next run |
| D6 | Hashing | md5; native md5 from the backend where available, else read |
| D7 | Incremental | mtime after watermark minus overlap; optional date-templated prefixes with lookback; no removals |
| D8 | System user | `heliopuddle:index` for created_by/updated_by written by the indexer |
| D9 | Deployments | `index-full-p<pid>-sl<slid>-<p>-<sl>` and `index-incremental-p<pid>-sl<slid>-<p>-<sl>` (SVC-13), cron from config; stale ones removed on start |
| D10 | Adoption | First start submits `index_full` for storage locations never fully indexed |
| D11 | Concurrency | `storage-location:<p>:<sl>` limit; full waits, incremental skips when busy |
| D12 | Repair | Stale `pending` re-verified, stale `deleting` retried by the full index |
| D13 | Run summary | Counts returned and published as a Prefect markdown artifact; progress saved only on success |
| D14 | Run endpoint | `POST /projects/{p}/runs {kind: index}` → `202 {flow_run_id, flow_run_url}` |

### M3 follow-up decisions (generic storage, dependencies, hashing)

Approved plan: [reviews/m3-followup-plan.md](reviews/m3-followup-plan.md).

| # | Topic | Decision |
|---|---|---|
| G1 | Generic by default | Only the generic fsspec API; backend knowledge only in `storage/backends.py`, enforced by a test |
| G2 | Temp writes | Superseded by H2: `temp_writes` removed; new files written directly under a reservation, overwrites staged and published |
| G3 | Roots | Superseded by E2: `create_root: auto` creates directory-style roots, not buckets/containers |
| G4 | Native md5 | Indexing shortcut only; verify and accept always read |
| G5 | Hash | md5, computed in Python |
| G6 | Backends | Extras `s3`, `azure`, `gcs`, `sftp`, `smb`, `all`; Docker installs `all` |
| G7 | Conformance | One suite for every test backend (memory, local, Azurite) |
| G8 | Dependencies | Lockfile on current releases (`uv lock --upgrade` at each milestone start and before large PRs; `scripts/check_deps.py`); loose lower bounds in `pyproject.toml`, only where needed (amended M6) |

### M3 follow-up 2 decisions (sensible defaults)

Approved plan: [reviews/m3-followup2-plan.md](reviews/m3-followup2-plan.md).

| # | Topic | Decision |
|---|---|---|
| E1 | Principle | Easy setup: minimal config, sensible defaults, user can override (CLAUDE.md) |
| E2 | Roots | `create_root: auto \| true \| false`; `auto` creates directory-style roots, not buckets/containers |
| E3 | When | Roots checked/created at startup (fail fast) and before each write |
| E4 | Zero config | No project files → `<data dir>/default_config.yaml` is written (if missing) and loaded: `default` project with `primary` and `archive` at `file://<data dir>/{primary,archive}-storage`, version rules and an hourly archive rule |
| E5 | Docs | Quick start is just `server start`; defaults table in deployment.md |

### M3 hardening decisions (operation ownership)

After an independent review reproduced lifecycle defects. Approved plan:
[reviews/m3-hardening-plan.md](reviews/m3-hardening-plan.md); the contract is
in [operations](../design/operations.md).

| # | Topic | Decision |
|---|---|---|
| H1 | Ownership | An operation owns a copy via its state (`pending` with a token, `overwriting`, `deleting`) plus `op_token`/`op_deadline`; others get `409 busy` |
| H2 | Writes | `temp_writes` removed: new files written directly by the reservation holder; overwrites staged to a unique `.hp-tmp` and published so the original or the complete new content is always in place; failed sources abort, never commit |
| H3 | Guarded transitions | Every state change carries the expected `generation` and is checked under the series lock |
| H4 | Delete | Removes a store object only if it matches hp's last observation |
| H5 | URL change | "The disk moved": copies keep hash/size, become `pending`, adoption re-checks them |
| H6 | Identity | Deployment names include project and storage-location ids |
| H7 | Robustness | Safe incremental watermark, failure semantics, consistent observations, streaming index and SQL latest selection, idempotent concurrent starts, series-key namespace, stricter config validation, clean split-port shutdown |

## M5A review decisions

| # | Topic | Decision |
|---|---|---|
| A1 | Numbering | M5A sits between M5 and M6; M5 and M6–M8 are not renumbered; the old "M9 Auth" row is replaced; requirements use `AUTH-xx` only |
| A2 | Default | `local` mode, secure by default; proxy deployments set `HELIOPUDDLE_AUTH_MODE=header` (upgrade note) |
| A3 | Authorization | Realm roles (`realm_access.roles`), not OAuth scopes; global roles plus optional `<role>:<project>` roles |
| A4 | Implied read | Writer, deleter and operator include read access |
| A5 | Users | CRUD through the API (`hp-admin`); the CLI only calls the API; admin recovery by env flag, not a DB command |
| A6 | Tokens | 8h, no refresh tokens; roles re-read from the DB per request in `local` mode |
| A7 | Hidden projects | A caller with no role on a project gets `404`, with some role but not the needed one `403` |
| A8 | Passwords | argon2, minimum 12 characters |
| A9 | Tests | Fake OIDC issuer in unit tests; one manual Keycloak test; no automated Keycloak or DEP-14 e2e |

## M6 review decisions

| # | Topic | Decision |
|---|---|---|
| U1 | Numbering | The UI redesign is M6; sync targets, analysis and post-ingest jobs move to M7–M9 (STO-05, ANA-xx, ING-xx move with them) |
| U2 | Design | Concept "C2 · Observatory revised": light theme under a dark starfield header; Files page from concept A (views, filter chips, a state column per storage location, inspector); no command palette, a plain file search instead |
| U3 | Storage locations | 1–5 per project is typical, not a limit: every page must stay usable with more (Files shows the first 6 as columns plus a column chooser; the rest stay reachable) |
| U4 | JavaScript | The basics of each page work without it (content, links, GET filters and paging, POST forms); enhancements may need it (typeahead, in-page inspector, column chooser) |
| U5 | Search | One cross-project endpoint over the caller's readable projects; plain-text multi-word match on the path; SQL `LIKE`, no trigram index; unknown and hidden named projects give the same `404` |
| U6 | Summary | One read-only summary endpoint per project, computed by aggregate SQL; no new tables or caches |
| U7 | Runs | No Activity page; "Runs" links to the Prefect UI |
| U8 | Dropped figures | No "% replicated" (rules deliberately leave some locations without copies) and no metadata filter |
| U9 | Heatmap | A state column that is zero in every storage location is left out |
| U10 | Previews | Built by the API on the server (the UI stays HTTP-only) from the first readable copy; bounded reads so files of hundreds of MB preview quickly; first 100 rows; cached in memory per copy |
| U11 | CDF | Read with `crump` (0.8: bounded reads of the first 100 records, variables grouped into tables by record count, array columns named from labels, unreadable variables reported rather than fatal); a CDF on storage without local paths is copied to a temp file up to `HELIOPUDDLE_PREVIEW_MAX_BYTES` |
| U12 | Dependencies | `crump` (with `cdflib`, `pyarrow`), `ijson` and `markdown-it-py` are core dependencies so previews work out of the box |
| U13 | Docker image | One `docker/Dockerfile` for the e2e tests and Docker Hub; defaults need no settings (state in `/var/lib/heliopuddle`, files in `/data`, projects in `/config`); external Postgres, Prefect and OIDC by env vars only; no test tooling in the image |
| U14 | Image tags | Tag `vX.Y.Z` (matching pyproject) → `X.Y.Z`, `X.Y`, `X` (from 1.0), `latest` (not pre-releases); push to `main` → `main`; manual run → `dev-<branch>-<date>-<sha>` and `dev-<branch>` only; amd64 and arm64 |
| U15 | Client install | The core install is the client only (`fsspec`, `httpx`, `pydantic`, `pydantic-settings`, `pyyaml`) so an application with its own Prefect, typer or crump pins can embed it; the server's dependencies (including the U12 preview packages) move to the `server` extra, which `all`, the Docker image and the dev group install |
| U16 | Register from the client | `register_file()` wraps the existing `POST /projects/{p}/files` for applications that write the store themselves; no new endpoint |
| U17 | Test harness | An in-memory fake client in `heliopuddle.testing` (client install only) plus a `pytest11` plugin; fidelity is guarded by one contract test suite run against both the fake and the real API rather than by mocking HTTP |
| U18 | File CLI | `heliopuddle file ...` over the client only (no DB access, like `heliopuddle user`); a `cli` extra (`typer`) so it installs without the server. Files are chosen by id (a number is always an id) or by `--path`/`--name` globs, so a numeric path is never mistaken for an id. The version is not settable: it comes from the path (API-02) |
| U19 | Client floors | Old, tested lower bounds for the client's packages, so applications with older pins can install it; `tests/client` runs at the floors in CI. Elsewhere bounds are loose and the lockfile is what tracks current releases (DEP-09 amended) |

## M7 review decisions

| # | Topic | Decision |
|---|---|---|
| K1 | SFTP | Already supported through fsspec's `sftp://` (paramiko is only its dependency); M7 adds a real-server conformance test and docs, no SFTP code |
| K2 | Mirror rules | Dropped: the existing copy, move and delete rules cover them |
| K3 | Credentials | Inline, `${VAR}` and backend-native env vars, Prefect Secret blocks via `{{ prefect.blocks.secret.<name> }}`, and Prefect `RemoteFileSystem` blocks via `block:` |
| K4 | Missing blocks | Never block startup (the user needs the server running to create them): the storage location is `incomplete`, the API and UI show why, and blocks resolve lazily (60s cache) so no restart is needed |
| K5 | rclone shape | A new fsspec backend `rclone://`, not a separate storage-location type; hp's own thin client over `rclone rcd` (existing packages buffer whole files or are unmaintained) |
| K6 | rclone config | The prefect-managedfiletransfer `RCloneConfigFileBlock` pattern (or a file): written to a private temp config per session, saved back afterwards even after a failure |
| K7 | Single rcd | One rcd per rclone config across the cluster, enforced by a Prefect global concurrency limit `rclone:<config>`; normally one per project; started locally where and when needed, never a shared rcd container |
| K8 | rcd security | Unix socket in a private directory, random per-session credentials, no GUI or metrics, an allowlist of rc endpoints, killed with hp |
| K9 | Verify | `verify: auto` (default): the provider's own hash (Box SHA1, OneDrive QuickXorHash) compared with the same hash of the local spool; content read-back when the provider has none. Amends STO-03 for rclone |
| K10 | Tests | No real Box or SharePoint: rclone over a WebDAV container, plus a fake local OAuth token endpoint for token write-back (rclone runs on the same machine, so no container is needed) |
| K11 | Binary | Bundled in the image; otherwise downloaded at a pinned version with pinned SHA256s (`HELIOPUDDLE_RCLONE_AUTO_DOWNLOAD`) |
| K12 | First storage location | rclone cannot be a project's first storage location in M7 (lock contention on interactive requests, SharePoint changing Office files); revisit later |
| K13 | Partial storage locations | `path_prefix: <folder>`: a storage location's root holds one project folder; paths stay project paths everywhere, the mapping is only in `StorageLocation`; rules skip files outside a destination's prefix; prefixed storage locations are reached through the API (`direct_access: false`) |

# Architecture

hp is one Python package, `heliopuddle`, laid out like Prefect: a client that
speaks HTTP, and a server with api, database, models, schemas, services and ui.

```
src/heliopuddle/
  settings.py        pydantic-settings, prefix HELIOPUDDLE_; fail-fast validation
  config/            project YAML models (package `models/`: common, versioning, rules,
                     project; all names re-exported from `config.models`) + loader
  client/            HelioPuddleClient (httpx, sync + async), fsspec writer, hashing;
                     `client.py` composes mixins (`_base`, `_users`, `_uploads`, `_files`,
                     `_rules`, `_content`) so the public method surface is one class
  testing/           FakeHelioPuddleClient and pytest fixtures for applications that call hp
  storage/           fsspec storage location wrapper (any fsspec backend; backend
                     knowledge isolated in storage/backends.py, see storage.md); later sftp.py, rclone/ (bundled binary)
  versioning.py      path -> (series_key, version_number | version_text) from per-path rules
  server/
    api/             FastAPI app: one folder per resource, one module per endpoint
      files/ uploads/ auth/ users/ projects/ rules/ search/ health/
                     (each: `__init__.py` exports `router`; `_shared.py` for shared helpers)
      runs.py        single-endpoint resource: one file
      deps.py        shared dependencies (context, principal, `requires`, project resolvers)
      errors.py      shared error responses and exception handlers
      repair.py      register/reserve write loop: repair an expired operation, map errors
    context.py       AppContext (settings, loaded projects, engine, sessionmaker); get_context()
    database/        SQLAlchemy 2 engine/session (SQLite + Postgres), packaged alembic migrations
    models/          DB access functions (files, storage locations, series), row locks;
                     `files/`, `index/` and `uploads/` are packages split by responsibility
    schemas/         pydantic API schemas (files, uploads, projects, runs, health, search,
                     preview, auth, rules, reconcile), shared with the client; modules
                     mirror the API resources
    services/        Prefect flows and tasks; deploy*.py; concurrency helper;
                     index.py (flows) with index_run/index_tasks/index_diff/
                     index_finish/index_models
    ui/              FastAPI + Jinja2, small vanilla JS enhancements (see ui.md); talks to the API via heliopuddle.client
  preview/           bounded content previews (CSV, JSON, text, Parquet, CDF; M6)
  pipelines/         stage framework (M8)
  cli.py             `heliopuddle server start ...`
  cli_entry.py       the console script: install hint when the cli extra is missing
```

## Components and boundaries

```
 client (any process) ──HTTP──▶ api ──▶ models ──▶ database
        │                        │
        └──fsspec──▶ store       └──run_deployment──▶ Prefect ──▶ services ──▶ models, storage
 ui ──heliopuddle.client──▶ api
```

- **client** knows only the API URL. It fetches the storage location config (URL and
  storage options) from the API and reads or writes the store directly
  (`hp-client-storage-read-write` on the project, or `hp-admin`, required in
  `local` and `oidc` modes; see [auth](auth.md)). Without that permission it
  streams content through the API using reader/writer permissions. It
  never imports server code that touches the DB; an import-linter contract
  (`lint-imports`, run in CI) forbids `heliopuddle.client` and
  `heliopuddle.server.ui` importing `heliopuddle.server.database`,
  `heliopuddle.server.models` or `sqlalchemy`. The client's imports need only
  the core install (`fsspec`, `httpx`, `pydantic`, `pydantic-settings`,
  `pyyaml`); everything else the server uses comes from the `server` extra
  (DEP-17), so other applications can embed the client. An application that
  writes the store itself calls `register_file()` (`POST /projects/{p}/files`)
  instead of `add_file()`.
- **testing** (`heliopuddle.testing`) is the harness for applications that call
  hp: `FakeHelioPuddleClient`, an in-memory stand-in for the client's file
  methods, and a pytest plugin (`heliopuddle_fake`, `heliopuddle_store`
  fixtures). It imports only client code. `tests/unit/test_testing_fake.py`
  runs one contract suite against both the fake and the real API.
- **cli** (`cli.py`, `cli_user.py`, the `cli_file/` package): `server start` and
  `db upgrade` import the server lazily; `user ...` and `file ...` use only
  the client, so they work from `heliopuddle[cli]` on any machine.
  The console script is `cli_entry.main`, which prints how to install the
  `cli` extra (instead of a traceback) when `typer` is missing.
- **api** validates requests, calls `models`, and submits flow runs. It does
  no bulk work inline (listing, bulk copying/hashing). It verifies a single
  registered file by running the `verify_file` task's
  function inline (`verify_file.fn`, no Prefect task run) so the register
  response carries the verified state. The API content routes stream one file,
  calling `open_file_content.fn` or `write_upload_content.fn`; these bounded
  transfers do not require backend credentials on the client.
  `DELETE /files/{id}` and `accept` do the same with `delete_file` and
  `accept_copy`. Flows run the same tasks as
  Prefect tasks.
- **models** are plain functions taking a SQLAlchemy session. They own row
  locking and `is_latest` maintenance.
- **services** are Prefect flows and tasks. They are the only place where
  store-side work (verify, index, copy, move, delete) happens on the server.
  Per-file work is a task; flows compose tasks over many files. The API may
  call a per-file task directly.
- **ui** uses `heliopuddle.client` so that it can run as its own process.
  `create_ui(settings, client_factory=None)` builds the UI app (routes
  relative to the UI root: `/`, `/projects/{p}`, `/projects/{p}/files/{id}`,
  `/static/...`). The file list reads `client.find(...)`; its filters are GET
  query parameters (`path` glob, `latest_only`, `state`, `include_deleted`,
  checkboxes on for `1`/`on`/`true`/`yes`) carried into the paging links. An
  unknown `state` or a non-integer `limit`/`offset` renders a `400` page. The
  detail page reads `client.get_file(id)` (a file of another project is a
  `404`) and `client.list_versions(series_id, include_deleted=True)`. Each
  request builds a client for `HELIOPUDDLE_UI_API_URL`, or by default the API
  in the same process over loopback
  (`http://127.0.0.1:{PORT}{API_ROOT_PATH}/api/v1`), and forwards the
  caller's user header. `HELIOPUDDLE_UI_ROOT_PATH` is set as the ASGI root
  path, and every link and redirect is a root-relative path that includes it.
  Pages are server-rendered Jinja2 and work without JavaScript. API errors
  render an error page with the API's status; an unreachable API gives `502`.
  From M3 the project page also has a storage-locations panel
  (`client.list_storage_locations()`: URL, whether external changes are
  accepted, last full and incremental index times) with "Index now" buttons:
  a form `POST /projects/{p}/storage-locations/{sl}/index` (field `mode`,
  `full` or `incremental`) calls `client.trigger_index` and redirects (`303`)
  back to the project page with `index_run`, `index_sl` and `index_mode` query
  parameters. The page shows them as a notice with a link to the flow run in
  the Prefect UI (the run id must be a UUID; the link is built by the UI, not
  taken from the query). On the detail page each `unknown` copy of a live file
  has an "Accept as is" form, `POST /projects/{p}/files/{id}/accept` (field
  `storage_location`), which calls `client.accept_file` and redirects to the
  detail page. Live files with no `pending` or `available` copy are labelled
  "not latest-eligible" in the list and on the detail page.
  **Form posts and CSRF:** in `header` mode the user comes from an
  authenticating reverse proxy; `local` and `oidc` use a signed HttpOnly,
  SameSite=Lax session cookie ([auth](auth.md)). State changes are `POST`
  only, and a post is rejected with a
  `403` page when `Sec-Fetch-Site` is `cross-site`, or when `Origin` (else
  `Referer`) is present and its host is not the request's `Host` or one of its
  `X-Forwarded-Host` values (`Origin: null` is rejected). A post with neither
  header is allowed. A proxy in front of the UI must pass `Host` through or
  set `X-Forwarded-Host`.
- **config** is read from YAML and is read-only at runtime.

### API package layout

`server/api/` has **one endpoint per file**. A resource with several endpoints is a
folder (`api/files/register.py`, `check.py`, `by_path.py`, ...); each endpoint module
defines its own `router = APIRouter()` with its route(s), and the folder's
`__init__.py` builds the resource `router` (tags) and `include_router`s the endpoint
routers **in route registration order**, which FastAPI matches in order (put a
literal path before a `{param}` path that could swallow it). `create_api()` includes
each resource `router`. A resource with a single endpoint stays one file
(`runs.py`). Code shared by several endpoints of a resource lives in that folder's
`_shared.py`; code shared across resources lives in `deps.py` and `errors.py`;
helpers used by one endpoint stay in its module. Keep every module under about 300
lines; split a large endpoint into a folder.

Pydantic schemas deliberately stay **out of `api/`**, in `server/schemas/`, one module
per resource mirroring the API folders. `heliopuddle.client` imports them, and the
client must never import FastAPI or the DB layer (see the import-linter contracts),
which importing them from `api/` would force. `heliopuddle.server.schemas` re-exports
every public schema.

## Runtime

`heliopuddle server start` (`cli.py` → `server/start.py`; the uvicorn servers, shutdown
handling and `StartupError` are in `server/serving.py`) runs, in one process:

1. Load settings (`--host`/`--port` override them); exit 2 naming the
   `HELIOPUDDLE_*` variable on an invalid one. Reject option combinations that
   cannot work (nothing enabled, UI without API and no `HELIOPUDDLE_UI_API_URL`,
   two different root paths on one port) with exit 2.
2. Unless the process is UI only (`--no-api --no-services`): load all project
   configs (exit 2 naming the file and field on any error). With no project
   files in the config dir (or no config dir), write (if missing) and
   load `{DATA_DIR}/default_config.yaml`: project `default`, storage location
   `primary` and `archive` under `{DATA_DIR}`, with version rules and an hourly archive rule. Log one line saying so (DEP-10). Unless
   `--no-migrate`, run DB migrations (`alembic upgrade head`); with
   `--no-migrate`, fail if the schema is not at head. Then sync projects and
   storage locations from config into the DB, check every storage location
   root and create it where `create_root` allows (exit 2 naming the project,
   storage location and URL if it is missing and cannot be created; STO-08),
   and install the context (`set_context`). Absolute config dir, data dir and database URL are put in
   the environment so flow-run child processes see the same ones.
   (`heliopuddle db upgrade` runs only the migrations and exits.)
3. Prefect (`server/services/runtime.py`, skipped for a UI-only process):
   - `PREFECT_API_URL` set → `GET {url}/health`; exit 1 with a clear error if it
     fails.
   - unset → start `prefect server start` as a child process (own session, and
     on Linux it receives SIGTERM if hp dies) with `PREFECT_HOME` at
     `{DATA_DIR}/prefect` and its log in `{DATA_DIR}/prefect/server.log`; wait
     for `/health`; set `PREFECT_API_URL` in the environment and refresh
     Prefect's process-wide settings so every thread and child process uses it.
4. Unless `--no-services`: build the deployments (`server/services/deploy.py`)
   and serve them with a Prefect `Runner` (what `prefect.serve()` uses) in a
   background thread with its own event loop. Startup waits until the
   deployments are registered. Flow runs execute in child processes.
5. Unless both `--no-api` and `--no-ui`: run uvicorn (`server/app.py`,
   `create_app`) with the API mounted at `/api/v1` and the UI at `/`. If
   `HELIOPUDDLE_UI_PORT` is set and differs from the API port, the UI runs on a
   second uvicorn server in the same process (same event loop). With
   `--no-api`, the UI talks to `HELIOPUDDLE_UI_API_URL`. With neither, the main
   thread waits for a signal.
6. On SIGTERM/SIGINT: stop uvicorn, the served deployments, then the embedded
   Prefect server, and exit 0 (a second SIGINT forces uvicorn to stop at
   once). If the served deployments stop on their own, the process exits 1.

## Queueing and concurrency

- **All background work is a Prefect flow run.** (Verifying the single file
  in a register request runs the task's function inline: no task run, and
  no Prefect server involved.) The API submits with
  `run_deployment(name, parameters, timeout=0)` and returns the flow run id.
  If a submission is accepted, Prefect owns completion and retries. hp has no
  queues, journals, run tables or change feeds of its own.
- **Runs endpoint (M3):** `POST /projects/{p}/runs {kind: "index",
  storage_location?, mode}` (`server/api/runs.py`) checks the storage location
  (default: the project's first; unknown `404`, inactive `409`), then
  `server/services/runs.py: submit_index_run()` calls
  `run_deployment("<flow>/index-<mode>-<p>-<sl>", timeout=0, as_subflow=False)`
  and answers `202 {flow_run_id, flow_run_url, deployment}`. `flow_run_url` is
  `<Prefect UI>/runs/flow-run/<id>`, where the Prefect UI is
  `HELIOPUDDLE_PREFECT_UI_URL` or `PREFECT_API_URL` without `/api`
  (`heliopuddle.settings.prefect_ui_url()`; null when neither is set). A missing
  deployment (the services have not registered it) or an unreachable Prefect
  gives `503` with the deployment name in the message.
- **Global limits:** `hp_concurrency(name)` wraps Prefect's
  `concurrency()` context manager; names are `storage-location:<project>:<name>` and
  `rule:<project>:<name>` (Prefect limit names cannot contain `/`). Deployments whose runs must not overlap use
  `concurrency_limit=1`. `hp_concurrency(..., wait=False)` tries once and
  raises `ConcurrencyBusyError` if the limit is full (the incremental index
  uses it to skip a run). `ensure_concurrency_limit(name)` creates a missing
  limit with one slot; the index flows call it before taking their lock, so
  the storage-location limit exists without any setup.
- **Row locks:** per-series and per-file updates lock rows
  (`SELECT ... FOR UPDATE` on Postgres). On SQLite (WAL mode) reads are plain
  deferred transactions and write transactions start with `BEGIN IMMEDIATE`
  (through `write_session()` in `server/database/engine.py`, or
  `AppContext.write_session()`; it marks the connection with an execution
  option that the SQLite `begin` hook reads), so writers are serialised and
  `FOR UPDATE` is a
  no-op. (In M1 every SQLite transaction was `IMMEDIATE`.) Store-side work (hashing, copying) runs outside DB
  transactions so the lock is held only briefly.
- **Context:** flows, tasks and the API reach config and the DB through
  `heliopuddle.server.context.get_context()`; a task called with only an id
  (such as `verify_file`) sees the same config and DB as its caller.

## Settings (env)

| Setting | Default | Milestone |
|---|---|---|
| `HELIOPUDDLE_CONFIG_DIR` | `./config`; no project files there → generated `{DATA_DIR}/default_config.yaml` (M3) | M1 |
| `HELIOPUDDLE_DATA_DIR` | `./.heliopuddle` (`/var/lib/heliopuddle` in Docker) | M1 |
| `HELIOPUDDLE_DATABASE_URL` | `sqlite:///{DATA_DIR}/heliopuddle.db` | M1 |
| `HELIOPUDDLE_HOST` / `HELIOPUDDLE_PORT` | `127.0.0.1` / `8080` (`0.0.0.0` in Docker) | M1 |
| `HELIOPUDDLE_USER_HEADER` | `X-Forwarded-User`; used only when `AUTH_MODE=header` | M1 |
| `HELIOPUDDLE_AUTH_MODE` | `local` (`oidc`, `header`); [auth](auth.md) | M5A |
| `HELIOPUDDLE_ADMIN_USERNAME`, `HELIOPUDDLE_ADMIN_PASSWORD` | `admin` / generated and logged once; applied only while there are no users | M5A |
| `HELIOPUDDLE_ADMIN_RESET` | `false`; with the password set, resets the admin at startup | M5A |
| `HELIOPUDDLE_AUTH_SECRET_KEY` | generated into `{DATA_DIR}/auth_secret`; if set it must be at least 32 characters (an empty or short value stops startup) | M5A |
| `HELIOPUDDLE_AUTH_TOKEN_TTL` | `8h` | M5A |
| `HELIOPUDDLE_OIDC_ISSUER`, `HELIOPUDDLE_OIDC_CLIENT_ID`, `HELIOPUDDLE_OIDC_CLIENT_SECRET` | none; issuer and client id are required in `oidc` mode, the secret unless introspection is `off` | M5A |
| `HELIOPUDDLE_OIDC_AUDIENCE` | the client id | M5A |
| `HELIOPUDDLE_OIDC_USERNAME_CLAIM`, `HELIOPUDDLE_OIDC_ROLES_CLAIM` | `preferred_username`, `realm_access.roles` | M5A |
| `HELIOPUDDLE_OIDC_INTROSPECT` | `sensitive` (`off`, `always`) | M5A |
| `HELIOPUDDLE_API_URL` | the API the `heliopuddle user` CLI calls; `http://127.0.0.1:8080/api/v1` | M5A |
| `HELIOPUDDLE_USERNAME`, `HELIOPUDDLE_PASSWORD`, `HELIOPUDDLE_TOKEN` | the account the CLI logs in as (password prompted if unset), or a bearer token instead; CLI only, not server settings | M5A |
| `HELIOPUDDLE_NEW_PASSWORD` | the new user's password for `user add` / `user set-password` (else prompted); CLI only | M5A |
| `HELIOPUDDLE_PREFECT_UI_URL` | derived from `PREFECT_API_URL` | M1 |
| `PREFECT_API_URL` | unset → embedded server | M1 |
| `HELIOPUDDLE_UI_API_URL` | same process | M1 |
| `HELIOPUDDLE_UI_HOST` / `HELIOPUDDLE_UI_PORT` | as API (shared port) | M1 |
| `HELIOPUDDLE_UI_ROOT_PATH` / `HELIOPUDDLE_API_ROOT_PATH` | empty | M1 |
| `HELIOPUDDLE_OP_LEASE` | `1h`: how long an upload, overwrite or delete owns a path before it expires and may be repaired ([operations](operations.md#expiry-and-repair)) | M3 |
| `HELIOPUDDLE_PREVIEW_MAX_BYTES` | `1GB`: the largest CDF copied to a temporary file to preview it from storage without local paths ([preview](preview.md)) | M6 |

## API surface

See [`spec/requirements/api.md`](../spec/requirements/api.md). Summary:

| Area | Endpoints |
|---|---|
| Health | `GET /health`, `GET /version` |
| Projects | `GET /projects`, `GET /projects/{p}` |
| Storage locations | `GET /projects/{p}/storage-locations`, `GET /projects/{p}/storage-locations/{name}` |
| Files (read) | `GET /projects/{p}/files` (filters, paging), `GET /files/{id}`, `GET /files/by-path`, `GET /series/{id}/versions` |
| Search, summary and preview (M6) | `GET /search/files` (across readable projects), `GET /projects/{p}/summary`, `GET /files/{id}/preview` |
| Files (write) | `POST /projects/{p}/files`, `POST /projects/{p}/files/check`, `DELETE /files/{id}`, `PATCH /files/{id}/metadata`, `PATCH /files/{id}/content-date`, `POST /files/{id}/accept` |
| Uploads (M3) | `POST /projects/{p}/uploads`, `POST /uploads/{token}/renew`, `POST /uploads/{token}/complete`, `DELETE /uploads/{token}` |
| Auth (M5A) | `POST /auth/token`, `GET /auth/config`, `GET /auth/me`, `POST /auth/me/password`; `GET/POST /users`, `GET/PATCH/DELETE /users/{username}` |
| Rules and runs | `GET /projects/{p}/rules`, `POST /projects/{p}/runs` |

## Extraction sources

Code is copied and adapted from earlier projects (no shims, no runtime
dependency):

| Origin | Used for |
|---|---|
| Datastore DB model, latest-version filter, engine cache and session pattern, fnmatch → LIKE | `server/database`, `server/models` |
| Datastore file manager: copy, md5 verify, delivery check, disk-space check | `storage`, operations |
| Indexed file manager and indexer flow | indexing, delete flow |
| Datastore cleanup flow and config | rules |
| Shared-docs upload flow | M7 mirror rule |
| File analysis feature branch, `data_pipelines` framework | M8 |
| Postgres upload and row-retention flows | M9 |
| Deployment and server-config patterns, duration parsing, datetime provider, disk space | `services/deploy.py`, utils |
| `prefect-managedfiletransfer`: transfer options, upload/download with temp file and size check, `ensure_space`, `FileMatcher`, `time_util`, `sftp_utils`, rclone command builder, invocation, config blocks and bundled binaries | `storage`, operations, M7 |

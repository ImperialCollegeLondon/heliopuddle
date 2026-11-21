# Deployment

hp runs as a Python process or a Docker container. Both use the same command.

## Python process

### Zero-config start and defaults

`uv run heliopuddle server start` works with no settings at all. With no
project files in `HELIOPUDDLE_CONFIG_DIR` (or no such directory), hp writes
`<data dir>/default_config.yaml` if it does not exist yet and loads it. It
defines a project `default` with storage locations `primary` and `archive` at
`file://<data dir>/primary-storage` and `file://<data dir>/archive-storage`
(created automatically), two version rules and a rule `auto-archive-old-versions-hourly`
that moves non-latest files modified over an hour ago from `primary` to `archive`. hp logs the file's path and
how to configure your own. The file is never overwritten, so you can edit it.
Any project file in the config dir replaces it.

| Setting | Default |
|---|---|
| Config dir (`HELIOPUDDLE_CONFIG_DIR`) | `./config`; if empty, the generated `<data dir>/default_config.yaml` |
| Data dir (`HELIOPUDDLE_DATA_DIR`) | `./.heliopuddle` (`/var/lib/heliopuddle` in Docker) |
| Database (`HELIOPUDDLE_DATABASE_URL`) | SQLite in the data dir |
| Prefect (`PREFECT_API_URL`) | embedded server started by hp |
| Host / port | `127.0.0.1:8080` (`0.0.0.0` in Docker); UI on the same port |
| User header | `X-Forwarded-User` |
| Storage location `create_root` | `auto` (local roots created; buckets/containers not) |
| Index | `batch_size` 1, `settle_time` 60s, no schedules |


```bash
uv sync
HELIOPUDDLE_CONFIG_DIR=./examples/projects uv run heliopuddle server start
```

Flags: `--host`, `--port`, `--no-api`, `--no-ui`, `--no-services`,
`--no-migrate`.

| Mode | Command |
|---|---|
| everything (default) | `heliopuddle server start` |
| API + services, no UI | `heliopuddle server start --no-ui` |
| UI only | `heliopuddle server start --no-api --no-services` with `HELIOPUDDLE_UI_API_URL` |
| services only | `heliopuddle server start --no-api --no-ui` |
| UI on its own port | `HELIOPUDDLE_UI_PORT=8081 heliopuddle server start` |
| migrations only | `heliopuddle db upgrade` (exits 0 on success) |
| skip migrations | `heliopuddle server start --no-migrate` (fails if the schema is not at head) |

- Flags override `HELIOPUDDLE_HOST`/`HELIOPUDDLE_PORT`; `--api/--ui/--services/--migrate`
  are the defaults.
- A UI-only process (`--no-api --no-services`) needs only settings: it reads
  no project config, opens no database and does not use Prefect. It listens on
  `HELIOPUDDLE_UI_HOST`/`HELIOPUDDLE_UI_PORT` (default `HOST`/`PORT`) and
  requires `HELIOPUDDLE_UI_API_URL`.
- Every other mode loads config, migrates (or checks) the database and needs
  Prefect: the API verifies registered files inline by calling the verify task's
  function directly (no task run).
- Shutdown (DEP-11): the first SIGTERM/SIGINT asks every uvicorn server
  (one, or two with the UI on its own port) to stop gracefully, once: open
  requests finish and each app's lifespan shutdown runs. If one server stops
  on its own, the others are stopped the same way. The main loop then waits
  only on the servers still running (no busy loop). Only a second signal
  forces an immediate exit.
- Exit codes: `0` after a clean shutdown (SIGTERM/SIGINT) or a successful
  `db upgrade`; `2` for invalid settings, project config or options (the
  message names the variable, or the file and field); `1` for anything else
  (migration failure, schema not at head with `--no-migrate`, Prefect
  unreachable or failing to start, the served deployments stopping).

## Docker

`docker/Dockerfile`, built from the repository root:
`docker build -f docker/Dockerfile -t heliopuddle .`

- Base: `python:3.12-slim` with uv; a builder stage runs
  `uv sync --frozen --no-dev --no-editable` into `/opt/venv`, which is copied
  into the runtime stage.
- Runs as the non-root user `heliopuddle` (uid 10001) under `tini`.
- Entrypoint `tini -- heliopuddle`, default command
  `server start --host 0.0.0.0 --port 8080`.
- Env: `HELIOPUDDLE_CONFIG_DIR=/config`,
  `HELIOPUDDLE_DATA_DIR=/var/lib/heliopuddle`.
- `PREFECT_UI_STATIC_DIRECTORY=/var/lib/heliopuddle/prefect/ui` keeps
  Prefect's generated UI assets writable by the non-root app user; the
  installed packages under `/opt/venv` remain read-only.
- Volumes: `/data` (default local store), `/config` (project YAML; the image
  ships `examples/projects` there as a default), `/var/lib/heliopuddle`
  (SQLite DB and the embedded Prefect DB), all owned by the app user.
- Exposes `8080` (API + UI) and the embedded Prefect UI port `4200`.
- **Security: the Prefect API on `4200` has no authentication.** Anyone who
  can reach it can run, cancel and delete flow runs and read their logs. Publish
  it on loopback only (`-p 127.0.0.1:4200:4200`, as every example here does) and
  never expose it on a public interface. For remote access put a reverse proxy
  with authentication in front of it, or use an SSH tunnel.
- Upload limit: `HELIOPUDDLE_MAX_UPLOAD_SIZE` (default `1GiB`) caps the size a
  reservation or registration may declare (`413` above it); content beyond the
  reserved size is already refused with `413` (API-23).
- Healthcheck: `GET /api/v1/health` (Python stdlib, no curl in the image).
- Behind a TLS-intercepting proxy, pass its CA bundle as an optional build
  secret: `docker build --secret id=ca,src=/path/to/ca-bundle.crt ...`.

```bash
docker run -p 8080:8080 -p 127.0.0.1:4200:4200 \
  -v $PWD/data:/data -v $PWD/examples/projects:/config \
  -v hp-state:/var/lib/heliopuddle heliopuddle/heliopuddle:latest
```

Clients write to the store themselves, using the storage location URL the API
returns (`hp-client-storage-read-write` on the project, or `hp-admin`, in
`local` and `oidc` modes, since these
connection details can include reusable credentials). With a local store that
URL is the **container's** path
(`file:///data`), so a client on the host can only write it if the same
directory is mounted at the same absolute path on both sides, e.g.
`-v /srv/hp-data:/srv/hp-data` with the project URL `file:///srv/hp-data`
(this is what the e2e tests do), or if the client runs in a container that
mounts the same volume at `/data`. A cloud storage location (S3, Azure, GCS)
has no such issue.
Clients without the credential role use API-based transfers instead and need
no local mount or backend credentials. When issuing project-scoped credentials,
restrict their permissions at the backend to that project's root.
When mixing direct and API-based writes to a local filesystem, the client and
server must have access to each other's newly created directories and files.
Use a shared service account or provision shared group permissions; hp does not
relax backend filesystem permissions.

### Docker image (Docker Hub and the GitHub container registry)

The published image is the same `docker/Dockerfile` the e2e tests build: no
test code or test tooling goes into it (`tests/` is not in the build context,
dev dependencies are not installed, and `docker/image_checks.py` checks every
image before it is pushed and in the e2e suite).

The image is published to Docker Hub as **`heliopuddle/heliopuddle`** (the one
the user docs use) and to **`ghcr.io/imperialcollegelondon/heliopuddle`**, with
the same tags: `:latest` is the newest release, `:X.Y.Z` and `:X.Y` a release,
and `:main` follows the main branch. `:pr-<number>` pull request builds are on
ghcr.io only (see the tags below). A fork publishes to its own
`ghcr.io/<owner>/<repo>`, and to Docker Hub only if it sets the variables below.

**Run it with no settings** and it is a complete standalone server:

| Path | Holds | Mount |
|---|---|---|
| `/var/lib/heliopuddle` | SQLite database, embedded Prefect database, auth signing key, staging | a named volume (keep it) |
| `/data` | files of the default storage location (`file:///data`) | the suggested place for local data: a volume, or a host directory writable by uid 10001 |
| `/config` | project YAML (an example project ships here) | your own directory of project files, read-only is fine |

```bash
docker run -d --name heliopuddle -p 8080:8080 -p 127.0.0.1:4200:4200 \
  -v hp-state:/var/lib/heliopuddle -v /srv/heliopuddle/data:/data \
  heliopuddle/heliopuddle:latest
docker logs heliopuddle 2>&1 | grep "first admin"     # the admin password
```

**External services are environment variables only** (see
`docker/heliopuddle.env.example` for every optional setting):

| Use | Set |
|---|---|
| External Postgres instead of SQLite | `HELIOPUDDLE_DATABASE_URL=postgresql+psycopg://user:password@host:5432/heliopuddle` (migrated at startup) |
| External Prefect instead of the embedded server | `PREFECT_API_URL=https://prefect.example/api`, and `HELIOPUDDLE_PREFECT_UI_URL` for the UI's links; startup fails fast if it is unreachable |
| OIDC / Keycloak instead of local users | `HELIOPUDDLE_AUTH_MODE=oidc`, `HELIOPUDDLE_OIDC_ISSUER`, `HELIOPUDDLE_OIDC_CLIENT_ID`, `HELIOPUDDLE_OIDC_CLIENT_SECRET` (see [Authentication](#authentication-m5a) and `examples/keycloak/`) |
| A fixed first admin, or several replicas | `HELIOPUDDLE_ADMIN_USERNAME`/`_PASSWORD`; replicas share `HELIOPUDDLE_AUTH_SECRET_KEY` |

`docker/compose.yaml` runs the standalone image; `docker/compose.external.yaml`
runs it against a Postgres and a Prefect server (drop those two services and
point the variables at your own in production). Both read optional settings
from `docker/heliopuddle.env`.

**Publishing** (`.github/workflows/docker.yml`, DEP-16) builds for
linux/amd64 and linux/arm64 and always pushes to the GitHub container
registry as `ghcr.io/<owner>/<repo>` (lowercase), so
`docker pull ghcr.io/<owner>/heliopuddle:main` works with no setup: the
workflow's `GITHUB_TOKEN` publishes the package and a source label links it
to the repository. The repository variable `GHCR_REPOSITORY` renames it. To
also push to Docker Hub, set the repository variable `DOCKERHUB_REPOSITORY`
(e.g. `myorg/heliopuddle`) and the secrets `DOCKERHUB_USERNAME` and
`DOCKERHUB_TOKEN` (an access token with read/write scope); every build then
goes to both registries with the same tags. The upstream repository sets
`DOCKERHUB_REPOSITORY=heliopuddle/heliopuddle`; its token is a personal access
token of a Docker ID with write access to that repository (organization access
tokens need a paid Docker plan), and `DOCKERHUB_USERNAME` is that Docker ID. Without that variable no Docker
Hub step runs. Pull request images go to ghcr.io only.

| Trigger | Tags |
|---|---|
| push tag `vX.Y.Z` (must equal the `pyproject.toml` version) | `X.Y.Z`, `X.Y`, `X` (from 1.0), `latest` (not for pre-releases such as `v1.2.0-rc1`) |
| push to `main` | `main` |
| manual run (Actions → Docker image → Run workflow, any branch, optional suffix) | `dev-<branch>[-<suffix>]-<yyyymmdd>-<sha>`, `dev-<branch>[-<suffix>]` |
| pull request to `main` (opened or pushed to) | `pr-<number>`, `pr-<number>-<sha>` (the PR's head commit); linux/amd64 only, ghcr.io only |

Manual and pull request builds therefore never move `latest`, `main` or a
version tag. A pull request from a fork is built and smoke-tested but not
pushed, because its `GITHUB_TOKEN` cannot write packages. Old `pr-*` images are
not deleted automatically; remove them from the package page when no longer
needed. Every build is smoke-tested first: it starts with no settings and answers
`/api/v1/health`, and the image checks find no test tooling.

The same `vX.Y.Z` tag also publishes the Python package to PyPI
(`.github/workflows/pypi.yml`, https://pypi.org/p/heliopuddle). It builds the sdist and
wheel, checks the tag equals the `pyproject.toml` version, smoke tests the wheel,
then uploads with PyPI trusted publishing (no token secret) from the `pypi`
environment. Setup on pypi.org is a trusted publisher for owner
`ImperialCollegeLondon`, repository `heliopuddle`, workflow `pypi.yml`,
environment `pypi`. A manual run builds and checks without publishing.

## Prefect

- `PREFECT_API_URL` unset: hp starts `prefect server start` as a child process
  with `PREFECT_HOME={DATA_DIR}/prefect` (its SQLite DB and `server.log`),
  waits for its `/health` (up to 120 s), points itself and its child processes
  at it, and stops it on exit (SIGTERM, then SIGKILL after 20 s). It binds
  `PREFECT_SERVER_API_HOST` (default `127.0.0.1`; `0.0.0.0` in Docker) and
  `PREFECT_SERVER_API_PORT` if set, else `4200`, or a free port when `4200` is
  taken. On Linux the child also gets SIGTERM if hp is killed.
- `PREFECT_API_URL` set: hp checks `{PREFECT_API_URL}/health` at start and
  exits non-zero with a clear message if it fails.
- Deployments are built in `server/services/deploy.py` from code and config
  (limit and stale-deployment preparation is in `deploy_prepare.py`, adoption
  in `deploy_adopt.py`, the DB reads in `deploy_state.py`, the served runner
  in `deploy_runner.py`; `deploy.py` re-exports their public names):
  `verify_files` (deployment `verify_files/verify-files`; runs the
  `verify_file` task per copy), `delete_files` (M2; deployment `delete_files/delete-files`, runs the
  `delete_file` task per file, from `heliopuddle.server.services.delete`),
  `index_full`/`index_incremental` per active storage location (M3;
  deployments `index_full/index-full-p<project id>-sl<sl id>-<project>-<sl>`
  and `index_incremental/index-incremental-p<project id>-sl<sl id>-<project>-<sl>`,
  cron schedules from the
  project's `index:` config; index deployments no longer in config are deleted
  on start; a first-start `index_full` run adopts storage locations never
  fully indexed; see "Index deployments" below), `run_rule` per rule (M4, scheduled
  from the rule), `reconcile` per project (M5; see "Reconcile deployments" below). They are served by a Prefect `Runner` (the
  machinery behind `prefect.serve()`) in a background thread of the hp
  process, so no separate worker or work pool is needed. Entrypoints are
  module paths (`heliopuddle.server.services.verify.verify_files`), so they
  resolve from the installed package. The served list is data
  (`DEPLOYMENTS` in `deploy.py`: module, flow, deployment name); an entry
  whose module is not in the package is skipped. Flow runs execute in child processes
  that inherit the absolute `HELIOPUDDLE_CONFIG_DIR`, `HELIOPUDDLE_DATA_DIR`,
  `HELIOPUDDLE_DATABASE_URL` and `PREFECT_API_URL` of the hp process.
- `PREFECT_LOGGING_TO_API_WHEN_MISSING_FLOW` defaults to `ignore`: the API
  runs the per-file task functions inline, outside any run, so their logs
  stay in the process log. Inside a run, service code logs through Prefect's
  run logger so its INFO records reach the Prefect UI; see
  [Flow run logs](operations.md#flow-run-logs-svc-30) for what is logged and
  how to set `PREFECT_LOGGING_*` when you host your own Prefect server.
- Several `server start --no-api --no-ui` processes can serve the same
  deployments against one external Prefect server; Prefect hands each run to
  one of them.

### Index deployments (M3, SVC-09)

On `server start` with services, after Prefect is up (`deploy.py`):

1. **Limits.** For every storage location in config, the Prefect global
   concurrency limit `storage-location:<project>:<sl>` is created, or reset
   to 1. The index flows hold it (`hp_concurrency`): a full index waits,
   an incremental index skips when it is taken. When another process starting
   at the same time creates the limit first (a conflict), hp reads it again
   and updates it (SVC-18).
2. **Stale deployments.** Deployments of the flows `index_full` and
   `index_incremental` named `index-<mode>-...` (the current name format or
   the older `index-<mode>-<p>-<sl>`) that are not built from the current
   config are deleted; one that another process deleted meanwhile is fine. A
   served index deployment whose cron was removed from config has its old
   schedules deleted too (re-registering a deployment would keep them).
3. **Serve.** Each storage location gets
   `index-full-p<project id>-sl<sl id>-<p>-<sl>` and
   `index-incremental-p<project id>-sl<sl id>-<p>-<sl>` (SVC-13: the DB ids
   make the names unique, so `alpha-beta`/`primary` and `alpha`/`beta-primary`
   never collide; two specs with the same identity or name stop startup
   before anything is registered): parameters `{project, storage_location}`,
   tag `heliopuddle-index`, cron from `index.full_schedule` /
   `index.incremental_schedule` (none when omitted), and a deployment
   concurrency limit of 1, so runs of one deployment queue rather than
   overlap. If the index module is missing they are not served (a warning).
4. **Adoption (D10, SVC-18).** For each active storage location with no
   `index_progress.last_full_at` (read from the DB; a storage URL change
   deletes the row), an `index_full` run is submitted with
   `run_deployment(..., timeout=0)`, explicit parameters
   `{project, storage_location}`, the tag `heliopuddle-adoption` and the
   Prefect idempotency key `adopt-sl<sl id>-g<n>`. `n` comes from the Prefect
   run history, not the DB: while an adoption run of that deployment is
   unfinished its key is used again (so nothing new is created); otherwise
   `n` is one more than the highest `n` of earlier adoption runs of that
   storage location (1 if none). Several processes starting together compute
   the same key, so Prefect creates one run; after a failed adoption, or a
   URL change, the next start gets a new key. A failed submission is logged
   and does not stop startup; the next start tries again. The log ends with
   `Adoption runs submitted: <n>` (new runs only).

The runs API (`POST /projects/{p}/runs`) submits to the same deployments,
named from the storage location's ids, with explicit parameters.

A failure in steps 1–2 (Prefect refusing the requests) stops startup with
exit code 1.

### Rule deployments (M4)

Each configured rule gets `run_rule/rule-p<project id>-<project>-<rule>`,
with explicit project/rule parameters, optional YAML cron, concurrency 1 and
collision strategy `ENQUEUE`. The same startup lifecycle rejects duplicate
identities and removes stale rule deployments/schedules. Effective YAML/DB
enablement controls schedule activity. A single `rule-once` deployment accepts
validated unsaved specs and runs one full batch without retrigger.
It is always served as `run_rule_once/rule-once`. Rule schedules have the
stable slug `rule`; registration updates that schedule instead of adding
duplicates, while startup removes legacy or now-unconfigured schedules.
Startup reads `rule_state.enabled` without resetting it, then reconciles
schedule activity and disabled future runs again after registration.

Rule and index deployments schedule their own bounded continuation runs after
the configured pause, carrying cursors in Prefect parameters. No separate
queue, worker or hp run table is introduced. Rule disable persists runtime
state, pauses the schedule and cancels scheduled future runs; running batches
finish without scheduling another. See [rules](rules.md).
Rule continuation checkpoints remain on tagged Prefect flow runs so a
manual restart can resume a paused chain. Index checkpoints remain in
`index_progress`; neither mechanism adds a worker or custom run journal.

## Database

- SQLite by default at `{DATA_DIR}/heliopuddle.db` (WAL mode). hp uses a
  current SQLite bundled in the `pysqlite3` wheel (a core dependency on
  Linux x86-64, macOS arm64 and Windows, where wheels exist); on other
  platforms, e.g. Linux arm64 images, it falls back to the system `sqlite3`.
  Startup logs the SQLite version in use. Within one process, open hp's
  database only through hp's engine: two copies of SQLite in one process do
  not see each other's file locks (Prefect's own SQLite database is a
  separate file, so it is unaffected). Planner statistics are refreshed
  with a bounded `ANALYZE` at startup and after each finished full index.
- Postgres (M2) via `HELIOPUDDLE_DATABASE_URL=postgresql+psycopg://...`.
  The `psycopg` (v3) driver is a dependency; a plain `postgresql://` URL also
  uses it. hp needs its own database (or schema owner rights); migrations run
  on start as for SQLite. The engine pre-pings pooled connections and
  recycles them after 30 minutes (pool size 5, overflow 10, per process).
- Migrations run on every start (`alembic upgrade head`) unless
  `--no-migrate`; a failure stops startup. `heliopuddle db upgrade` runs them
  alone, for example as an init container or a deploy step.
- On Postgres, migrating and syncing config take a transaction-level advisory
  lock (`pg_advisory_xact_lock`), so several hp processes can start against
  one database at once: one migrates, the others wait and then find the
  schema at head.


### Reconcile deployments (M5)

Each project gets `reconcile/reconcile-p<project id>-<project>`, with
`project`, `verify` and `rules` parameters from the project's `reconcile:`
config, the optional cron from `reconcile.schedule` (on demand only without
one), concurrency 1 and collision strategy `ENQUEUE`. A schedule has the
stable slug `reconcile`, so changing the cron updates it; removing it from
config removes it on start. The same startup lifecycle rejects duplicate
identities (`reconcile_deployment_specs`) and removes stale reconcile
deployments (`prepare_reconcile_services`, after the index and rule steps; the
flow's `reconcile:<p>` concurrency limit is held by the flow, so none is
created here). A missing `reconcile` flow module is skipped with a warning, like
other optional flows. `POST /projects/{p}/runs {kind: reconcile}` first checks
the deployment for an unfinished run (the same query as manual rule runs, in
`rule_control.unfinished_run`) and answers `409` naming it. See
[rules](rules.md#reconcile-m5).

### Postgres and an external Prefect server (M2)

Everything is configured by env vars; no flags or files change:

```bash
docker run -d --name hp-db -e POSTGRES_PASSWORD=hp -e POSTGRES_DB=heliopuddle \
  -p 5432:5432 postgres:16-alpine
docker run -d --name prefect -p 127.0.0.1:4200:4200 prefecthq/prefect:3.9-python3.12 \
  prefect server start --host 0.0.0.0
export HELIOPUDDLE_DATABASE_URL=postgresql+psycopg://postgres:hp@127.0.0.1:5432/heliopuddle
export PREFECT_API_URL=http://127.0.0.1:4200/api
export HELIOPUDDLE_CONFIG_DIR=$PWD/examples/projects HELIOPUDDLE_DEMO_DATA=$PWD/.heliopuddle/store
uv run heliopuddle server start
```

hp checks the external Prefect server's health at start (and exits if it is
down), migrates Postgres, then registers and serves `verify-files` and
`delete-files` against that server. No embedded Prefect is started and
nothing is written to `{DATA_DIR}/prefect`. The Prefect container is only the
API server: flow runs execute in child processes of hp, so the container
never needs to reach hp or its stores. Client adds and
`prefect deployment run verify_files/verify-files ...` (see "Trying it by
hand") work as with SQLite.

### Storage backends (M3)

Storage locations can use any fsspec backend; see [storage](storage.md) for
the supported backends, `storage_options` examples for S3, SMB, SFTP, Azure and
GCS, writes and staging, and `create_root` (why `auto` does not create buckets or
containers). The Docker
image installs `heliopuddle[all]` (the `server` extra plus every backend), so
all the target backends work in it. Clients need the matching backend extra
too, because they write to the store directly. A client-only install
(`pip install heliopuddle`, DEP-17) has no server dependencies and old,
tested dependency floors (DEP-18); add the
backend extras it writes to, e.g. `heliopuddle[s3]`. With a cloud backend no `/data` volume is needed, and objects already
in the bucket or container are adopted by the first-start `index_full` run.

#### Example: Azurite (the Azure emulator used by the tests)

Azurite (`mcr.microsoft.com/azure-storage/azurite`) uses a well-known
development account. hp does not create a container unless the storage
location sets `create_root: true` (as here). The
connection string's endpoint must work both inside the hp container and on
the host where clients run; one way is to let the hp container share Azurite's
network namespace and run Azurite on the same port number inside and outside,
so `127.0.0.1:<port>` means Azurite on both sides (this is what the e2e test
does):

```bash
docker run -d --name azurite -p 127.0.0.1:10000:10000 -p 8080:8080 -p 127.0.0.1:4200:4200 \
  mcr.microsoft.com/azure-storage/azurite \
  azurite-blob --blobHost 0.0.0.0 --blobPort 10000 --skipApiVersionCheck --loose
export AZURE_CONN="DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;"
# config/blob.yaml: url az://data/demo, create_root: true, connection_string: "${AZURE_CONN}"
docker run -d --name hp --network container:azurite -e AZURE_CONN \
  -v $PWD/config:/config -v hp-state:/var/lib/heliopuddle \
  heliopuddle/heliopuddle:latest
```

hp's ports are published on the Azurite container because the hp container
uses its network. With a real cloud account none of this is needed: the
endpoint is public and the hp container runs as usual.

## Authentication (M5A)

Design: [auth](auth.md). Defaults:

| Setting | Default |
|---|---|
| `HELIOPUDDLE_AUTH_MODE` | `local`: users in the DB, password flow |
| First admin | `admin`, with a generated password logged once (`docker logs`), or `HELIOPUDDLE_ADMIN_USERNAME` / `HELIOPUDDLE_ADMIN_PASSWORD` |
| Token lifetime | `8h` |
| Signing key | generated into `{DATA_DIR}/auth_secret`, or `HELIOPUDDLE_AUTH_SECRET_KEY` |

**Upgrading from before M5A:** requests were trusted by header. Behind a proxy
that authenticates users, set `HELIOPUDDLE_AUTH_MODE=header`; otherwise use the
default and create users with `heliopuddle user add`.

Quick start (no config): `uv run heliopuddle server start`, read the admin
password from the log, then say who you are and name the new user (the CLI
asks for the admin's password):

```bash
export HELIOPUDDLE_USERNAME=admin        # the account you log in as
uv run heliopuddle user add alice --roles hp-reader,hp-writer   # alice is the NEW user
```

`--username` (env `HELIOPUDDLE_USERNAME`) is always the account that runs the
command; the name after `add` is the user being created.

**Keycloak:** set `HELIOPUDDLE_AUTH_MODE=oidc` and the `HELIOPUDDLE_OIDC_ISSUER`,
`_CLIENT_ID` and `_CLIENT_SECRET` variables (the client id and secret from the
provider's client; see `examples/keycloak/`). In a container or compose file:

```yaml
environment:
  HELIOPUDDLE_AUTH_MODE: oidc
  HELIOPUDDLE_OIDC_ISSUER: https://keycloak.example/realms/heliopuddle
  HELIOPUDDLE_OIDC_CLIENT_ID: heliopuddle
  HELIOPUDDLE_OIDC_CLIENT_SECRET: ${HP_OIDC_SECRET}
```

Several hp processes (API replicas, or a UI on its own port or host) must share
`HELIOPUDDLE_AUTH_SECRET_KEY`.

## Reverse proxies

`HELIOPUDDLE_API_ROOT_PATH` and `HELIOPUDDLE_UI_ROOT_PATH` set the ASGI
`root_path` so hp works under a path prefix: generated links carry the
prefix, and requests are accepted both with it (the proxy keeps the prefix, or
a direct call such as the UI's default `http://127.0.0.1:{PORT}{API_ROOT_PATH}/api/v1`)
and without it (the proxy strips it). When the API and UI share a port they
share one root path: set one of the two, or both to the same value (two
different values fail startup); give the UI its own port to use a different
prefix. A proxy that authenticates users itself and forwards the user id in
`HELIOPUDDLE_USER_HEADER` needs `HELIOPUDDLE_AUTH_MODE=header` (see
[Authentication](#authentication-m5a)); in the default `local` mode the header
is ignored. With `oidc` and the UI behind a proxy, forward `Host` and
`X-Forwarded-Proto` so the login redirect URI is right.

## CI

GitHub Actions (`.github/workflows/ci.yml`), on every pull request:

| Job | What it runs |
|---|---|
| `lint` | `ruff format --check`, `ruff check`, `pyright` |
| `import-contracts` | `lint-imports` (CLI-04, UI-05); a violation fails CI |
| `unit` | `pytest tests/unit -n auto --dist loadfile` (pytest-xdist), Python 3.11–3.14; coverage ≥ 90% is measured on 3.14 only |
| `integration` | `pytest tests/integration` (Postgres and Azurite via testcontainers), in two parallel shards: `rules` (`tests/integration/rules`) and `server` (everything else) |
| `e2e` | builds the Docker image, runs `pytest tests/e2e` (container plus Playwright) |
| `requirements` | merges the suites' `--req-report` files and runs `scripts/req_matrix.py` |

Each pytest run writes a `--req-report`; under xdist the workers send their
results to the controller, which writes the one report. CI wall time is the
slowest job, so the unit suite uses every core and the slow integration suite
is split into two parallel shards. (`--dist loadgroup` is not used: pytest-xdist
3.8.0 ignores `xdist_group` under pytest 9.2.)

The same ruff, pyright and `lint-imports` checks run in pre-commit
(`.pre-commit-config.yaml`).

### Integration tests

`tests/integration` runs `heliopuddle server start` as subprocesses: one
shared server with the embedded Prefect on SQLite (`hp_server`), and one on
Postgres with an external Prefect, configured by env vars only
(`hp_postgres_server`, DEP-04). The Postgres (`postgres:16-alpine`) and
Prefect (`prefecthq/prefect:3.9-python3.12`) containers are started once per
session with testcontainers; `postgres_url` gives each test a fresh database.
On Postgres they cover the migrations (DB-01b), concurrent adds and removals
in one series (DB-02), and, against the external Prefect, versions, `DELETE`,
and `verify-files` and `delete-files` deployment runs (DEP-04, SVC-07).
The Postgres hp server is module-scoped: it must stop before the separate
rules acceptance server starts, since their different configs/databases
cannot share runners for the common `rule-once` deployment. The live
250-file rule chain passed on `03d3393` and was retired at the owner's
request; fast unit tests retain timestamp-tie and continuation coverage.
Small live tests verify downstream rule triggers after transfer publication
and direct expired-operation repair, without an index run masking the hook.
An Azurite container (`azurite`, connection string on the mapped port;
`az_container` gives each test a fresh container name) covers
`StorageLocation` on `az://` URLs (STO-02). A server of its own
(`hp_index_server`) starts on a store that already holds files and with index
schedules in config: index deployments, schedules and limits, the adoption
run on start, and an incremental run that skips while the storage location
lock is held while a full one waits (SVC-09).
Tests that need them are skipped when Docker is not available.
`HP_POSTGRES_IMAGE`, `HP_PREFECT_IMAGE` and `HP_AZURITE_IMAGE` override the
images (for example a registry mirror).

### e2e tests

`tests/e2e` builds the image (or uses `HP_E2E_IMAGE`) and runs one container
with temp host dirs at `/data` and `/config` and a fresh volume at
`/var/lib/heliopuddle`. The store dir is also mounted at its own host path,
and the test project's URL is `file://<that path>`, so the client on the host
writes to the same directory the server verifies. The suite is skipped when
Docker is not available. `HP_CHROMIUM` points Playwright at a local Chromium;
`HP_E2E_BUILD_ARGS` adds `docker build` arguments. The screenshot goes to
`.reports/screenshots/` and the container log to `.reports/e2e-container.log`.
The local-store fixture waits for both startup adoption runs to complete
before yielding, so background indexing cannot race the rules UI's strict
dry-run state comparison.

DEP-03 (`hp_azure`) runs a second container with an Azurite storage location
and no `/data` volume: Azurite listens on the same port number inside and on
the host, the hp container shares Azurite's network namespace, and the
connection string reaches hp as the env var `AZURE_CONN`, referenced from the
project YAML as `${AZURE_CONN}`. Blobs uploaded before hp starts are adopted,
a client add from the host ends `available` with the blob in Azurite, and a
blob deleted directly is soft-deleted by a full index. Its log goes to
`.reports/e2e-azure-container.log`.

## Trying it by hand

```bash
export HELIOPUDDLE_DEMO_DATA=$PWD/.heliopuddle/store HELIOPUDDLE_CONFIG_DIR=$PWD/examples/projects
uv run heliopuddle server start          # UI: http://127.0.0.1:8080, Prefect UI: http://127.0.0.1:4200
```

```python
from heliopuddle.client import HelioPuddleClient

with HelioPuddleClient("http://127.0.0.1:8080/api/v1", "demo", user="alice") as hp:
    print(hp.add_file("some_local_file.csv", "reports/2026/report_v001.csv").state)  # available
```

Re-verify in the background with a deployment run:

```bash
PREFECT_API_URL=http://127.0.0.1:4200/api uv run prefect deployment run \
  verify_files/verify-files --param file_storage_location_ids='[1]'
```

Stop with Ctrl+C or a single SIGTERM; a second signal forces an immediate exit.

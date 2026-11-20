# Deployment requirements (`DEP`)

**DEP-01 (M1)** `docker run` with volumes `/data`, `/config` and
`/var/lib/heliopuddle` runs the API, UI, services and embedded Prefect as a
non-root user under `tini`. `/api/v1/health` returns `200`, and a client add
from the host works end to end.
*Verify:* e2e test building and running the image.

**DEP-02 (M1)** `uv run heliopuddle server start` behaves the same as the container.
`--no-api`, `--no-ui`, `--no-services` and `--no-migrate` turn parts off, and
`--host`/`--port` override settings.
*Verify:* integration test.

**DEP-05 (M1)** Invalid settings or project config (bad YAML, schema errors,
duplicate names, unset `${ENV}`, a project with no storage location) stop
startup with a clear message naming the file and field, and a non-zero exit
code.
*Verify:* unit tests.

**DEP-04 (M2)** With only env vars set (`HELIOPUDDLE_DATABASE_URL=postgresql+psycopg://...`
and `PREFECT_API_URL` pointing at an external server), `server start` migrates
Postgres, registers and serves both deployments with the external Prefect, and
a client add, remove and `verify-files` deployment run work end to end.
*Verify:* integration test with Postgres and `prefecthq/prefect:3.9`
containers.

**DEP-06 (M2, amended M6A)** `versioning` rules are validated at startup:
`extraction_pattern` compiles; `version_type` is `number` (default) or `text`;
`version_text` is allowed only on `text` rules, names only groups of the
pattern (not `date`) and formats successfully with every group set to `"0"`;
`ignore_in_series` names groups of the pattern (not `date`); `paths` is a
non-empty list of globs; unknown keys (including the removed `pattern` and
`latest_by`) are rejected. An invalid rule stops startup naming the file, rule
index and field.
*Verify:* unit tests.

**DEP-03 (M3)** The same container works with an Azure (Azurite) storage location
instead of a volume, configured by env vars: blobs already in the container
are adopted on start, a client add (written straight to Azurite) ends
`available`, and a blob deleted directly is picked up by a full index.
*Verify:* e2e test with Azurite and the image.

**DEP-07 (M3)** The project `index` config is validated at startup: cron
expressions parse, `batch_size >= 1`, durations parse, and
`incremental_prefixes` templates use only `{date:<strftime>}`. An invalid value
stops startup naming the file and field.
*Verify:* unit tests.

**DEP-08 (M3)** The optional extras `heliopuddle[s3]` (s3fs),
`heliopuddle[azure]` (adlfs), `heliopuddle[gcs]` (gcsfs), `heliopuddle[sftp]`
(paramiko, for fsspec's `sftp://`), `heliopuddle[smb]` (smbprotocol, for
fsspec's `smb://`) and `heliopuddle[all]` exist; the Docker image installs
`all`, and every target protocol resolves inside the image.
*Verify:* unit test of the extras and protocols; e2e check running Python in
the image.

**DEP-09 (M3, amended M6A)** The lockfile is current: `scripts/check_deps.py`
reports no direct dependency locked behind its latest release except holds
documented in the script (each with its reason). Requirements in
`pyproject.toml` are loose: a lower bound is declared only where heliopuddle
or a dependency needs a newer release, or to exclude a release with a known
security issue (the M3 rule that lower bounds match the locked versions is
dropped). `uv lock --upgrade` runs at the start of every milestone and before
any large PR.
*Verify:* unit tests of the script. It is run by hand before a milestone or
large PR, not in CI: a dependency released after the last upgrade must not
fail an unrelated build (amended M6A).

**DEP-10 (M3)** Zero-config start: with no project files in the config dir (or
no config dir), `server start` writes `<data dir>/default_config.yaml`
(unless it already exists; never overwritten) defining a project `default` with
storage locations `primary` and `archive` at `file://<data dir>/primary-storage`
and `…/archive-storage` (created automatically), two version rules and an
hourly rule that moves non-latest files older than 1h from `primary` to
`archive`, loads it, and logs the file and how to configure your own; any
project file in the config dir replaces it. A client add works against it.
*Verify:* unit tests with an empty config dir (file written, loaded, not
overwritten; not written when project files exist); integration test starting
with no config, adding a file and listing it.

**DEP-11 (M3)** With the UI on its own port, one signal stops both servers
gracefully exactly once (lifespans run, no busy loop); only a second signal
forces exit.
*Verify:* unit test with two real uvicorn servers.

**DEP-12 (M3)** Config is validated for everything later assumed valid:
`incremental_prefixes` templates are parsed at load time (format syntax and a
strftime test); an invalid extracted version (a `number` value that is not
digits or too large, a `text` value that is empty or over 255 characters)
gives `422` naming the rule and value on register and reserve, and an
`unknown` copy with a logged reason in the indexer — never an unhandled error
(amended M6A).
*Verify:* unit tests.

**DEP-13 (M4, amended M6A)** Rule names are unique per project; source and destination
exist and differ; delete has no destination. Globs, durations, sizes, dates,
cron and positive batch limits validate at startup with the file/field named.
Storage-location quota accepts validated size strings. Every option has a
default: a minimal rule needs only name/action/source, destination unless
delete, and match paths. Rule `max_files` is replaced by `batch.max_files`.
From M6A it also rejects the removed match keys (`older_than`, `age_by`,
`content_date_after`, `content_date_before`), unknown `age` fields and age
ranges that can never match (an `older_than` duration not shorter than the
`younger_than` one, or an `older_than` date not later than the `younger_than`
one), naming the file, rule and field.
*Verify:* minimal config and table-driven invalid config tests, including
one-off validation through the same models.

**DEP-14 (M5)** Project config `reconcile: {schedule, verify, rules,
max_rule_passes, max_failures}` is optional with working defaults (no
schedule, no verify, all rules, 3 passes, 5 failures). Startup fails with the
file and field named for an invalid cron, `max_rule_passes` or
`max_failures` below 1, or a `rules` name that is not a configured rule.
*Verify:* minimal config and table-driven invalid config tests.

**DEP-15 (M6)** The image is configured only by environment variables. With
none set it starts on its own: local users, SQLite in the state volume
`/var/lib/heliopuddle` (with the embedded Prefect database and the auth
signing key), files in the `/data` volume (the suggested mount for local data)
and project YAML in `/config` (an example project unless a directory is
mounted there). `HELIOPUDDLE_DATABASE_URL` switches to an external Postgres,
`PREFECT_API_URL` (with `HELIOPUDDLE_PREFECT_UI_URL`) to an external Prefect
server, and `HELIOPUDDLE_AUTH_MODE=oidc` with the `HELIOPUDDLE_OIDC_*`
variables to an external OIDC provider such as Keycloak. `docker/compose.yaml`
(standalone), `docker/compose.external.yaml` (external Postgres and Prefect)
and `docker/heliopuddle.env.example` (every optional setting) show both, and
name only real settings.
*Verify:* unit tests of the examples and the Dockerfile; the e2e suite runs the
image with no settings (DEP-01); DEP-04 covers external Postgres and Prefect.

**DEP-16 (M6, amended M6A)** `.github/workflows/docker.yml` publishes the
image for linux/amd64 and linux/arm64 to the GitHub container registry,
always: `ghcr.io/<owner>/<repo>` in lowercase (the repository variable
`GHCR_REPOSITORY` renames it), logged in with the workflow's `GITHUB_TOKEN`
(`packages: write`), with an `org.opencontainers.image.source` label linking
it to the repository. Docker Hub is opt-in: when the repository variable
`DOCKERHUB_REPOSITORY` is set, the same tags also go to
`docker.io/<DOCKERHUB_REPOSITORY>`, which needs the secrets
`DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` (the build fails clearly if they
are missing); unset, no Docker Hub step runs.
A pushed tag `vX.Y.Z`, which must equal the `pyproject.toml` version, gets
`X.Y.Z`, `X.Y`, `X` (from 1.0) and `latest` (not for pre-releases such as
`v1.2.0-rc1`); a push to `main` gets `main`. A manual run on any branch gets
only `dev-<branch>[-<suffix>]-<yyyymmdd>-<sha>` and `dev-<branch>[-<suffix>]`,
and a pull request to `main` gets only `pr-<number>` and
`pr-<number>-<sha>` (its head commit; linux/amd64, ghcr.io only; a PR from a
fork is built and smoke-tested but not pushed), so development builds never
move `latest`, `main` or a version tag. The compose examples, README and
deployment docs run the canonical image
`ghcr.io/imperialcollegelondon/heliopuddle`. Every
build is smoke-tested before it is pushed: it starts with no settings and
answers `/api/v1/health`, and `docker/image_checks.py` finds no test tooling
(pytest, Playwright, testcontainers, ...) or test files in it. The e2e image
and the published image are built from the same `docker/Dockerfile`.
*Verify:* unit tests of the workflow's triggers and tag rules; e2e test running
`docker/image_checks.py` in the image.

**DEP-17 (M6)** The core install (`pip install heliopuddle`) is the client
only: its dependencies are `fsspec`, `httpx`, `pydantic`, `pydantic-settings`
and `pyyaml`, so another application can embed `heliopuddle.client` without
the server's dependency pins. The `cli` extra adds `typer` for the
`heliopuddle file` and `heliopuddle user` commands (CLI-15). The server and
the services need the `server` extra (which includes `cli`);
`heliopuddle[all]` is `server` and `cli` plus every storage backend extra, and
is what the Docker image and the dev group install.
*Verify:* unit tests of `pyproject.toml` and that `heliopuddle.client` imports
with every server-only package blocked.

**DEP-18 (M6)** **Client floors.** The core dependencies and the `cli` extra
are as loose as possible so the client, test harness and file CLI install
next to other projects' pins: `fsspec>=2023.1.0`, `httpx>=0.24.0`,
`pydantic>=2.5.0`, `pydantic-settings>=2.1.0`, `pyyaml>=6.0.1` and
`typer>=0.13.0`, with no upper caps. The `server` extra does not repeat
them (DEP-09: bounds only where needed). `scripts/client_floor.py` installs
exactly the floor versions (Python 3.11) and runs `tests/client`, which import
no server code; CI runs it. A floor is raised only when the client needs a
newer feature.
*Verify:* `tests/client` at the floors in CI (and in the normal unit run);
unit tests of the declared bounds, the floor script and `check_deps.py`.

**DEP-19 (M6A)** **Durations.** Every duration in config, settings and the CLI
is one or more `<number><unit>` parts, case-insensitive and optionally
separated by spaces, with units `s`/`sec`/`second(s)`, `m`/`min`/`minute(s)`,
`h`/`hr`/`hour(s)`, `d`/`day(s)`, `w`/`week(s)`, `mo`/`month(s)` (30 days) and
`y`/`yr`/`year(s)` (365 days): `30d`, `1h30m`, `2h 30m`, `90 days`, `1y6mo`.
`m` is minutes. An invalid duration fails with an example of the syntax.
Durations are written back as days, hours, minutes and seconds.
*Verify:* table-driven unit tests of parsing and round-trips.

**DEP-20 (M6A)** **Prefect stays on loopback.** The embedded Prefect API has no
authentication, so the compose files and every `docker run` example in the docs
publish it as `127.0.0.1:4200:4200`, and the deployment doc and README warn that
it must not be exposed publicly (use an authenticating reverse proxy for remote
access).
*Verify:* a unit test of the compose files and docs.

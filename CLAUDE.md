# CLAUDE.md

Guidance for working on heliopuddle (hp).

## Source of truth

- **Before any change, read the relevant docs** in `docs/spec` (what to build)
  and `docs/design` (how it is built).
- **Update the docs in the same commit as the code.** If code and docs
  disagree, fix one of them; never leave them out of step.
- Scope is set per milestone in `docs/spec/features.md`. Do not build features
  from a later milestone early.
- Requirement IDs live in `docs/spec/requirements/*.md`. Every requirement due
  in the current milestone needs at least one passing test tagged with it.

## Milestone process (every milestone until release 1.0)

The project owner expects every milestone to follow these steps, in order:

1. **Spec review.** Present the milestone spec as a plan that includes a table
   of every in-scope requirement with its full description. Do not build
   before the owner approves it.
2. **Docs first.** Update `docs/spec` and `docs/design` to match the approved
   plan, then commit and push.
3. **Build** test-first, delegated to agents with narrow briefs that cite the
   REQ IDs they own.
4. **Verify** end to end: unit and integration suites, coverage ≥ 90%, a
   complete REQ matrix, `docker run`, `uv run heliopuddle server start`, a
   client add, the UI and the Prefect runs.
5. **Open a PR to `main`** and ask the owner to test.
6. **Gate:** do not start the next milestone until that PR is merged to
   `main`. Then restart the working branch from the latest `main`.

## Layout

```
src/heliopuddle/
  settings.py   config/   client/   testing/   storage/   versioning.py
  server/{api,database,models,schemas,services,ui}/
  pipelines/    cli.py
tests/{unit,integration,e2e}/
docs/{spec,design,site}/
examples/   docker/
```

`docs/site` is the user documentation (MkDocs, `mkdocs.yml`), published to
GitHub Pages on every push to `main`; `docs/spec` and `docs/design` stay
internal. **Update `docs/site` in the same commit as any user-facing change**
(commands, settings, Docker, auth). The environment variable tables are
generated: `uv run python scripts/env_table.py --write` (a unit test checks
them). Build with `uv run --group docs mkdocs build --strict`.

The layout follows Prefect's: a client namespace that talks HTTP only, and a
server package with api, database, models (DB access functions), schemas
(pydantic API types shared with the client), services (Prefect flows) and ui.

## Conventions

- **uv** for everything: `uv sync`, `uv run pytest`, `uv add <pkg>`.
- Python 3.11+. Type hints everywhere; `pyright` clean; `ruff` for lint and
  format.
- **Google-style docstrings** on public functions and classes.
- **Config in YAML**, validated by pydantic, plus `HELIOPUDDLE_*` env settings.
  Durations are human-readable (`30d`, `12h`, `45m`, `60s`).
- **Easy to set up.** Minimal config with sensible defaults: every setting
  has a working default, initial setup assumes it, and the user can override
  it (e.g. `server start` with no config runs a built-in `default` project;
  local storage roots are created automatically). Check new settings against
  this in every spec review.
- **Fail fast:** invalid config, an unreachable external Prefect server or a
  failed migration stops startup with a clear message.
- **Keep it simple.** All queueing and background work is Prefect flow runs.
  Do not add custom queues, journals, run tables or change feeds.
  Concurrency comes from DB row locks plus Prefect global concurrency limits
  (via `hp_concurrency`).
- A source file is **never deleted before the destination hash is verified.**
- The client never touches the database. The UI talks to the API only through
  `heliopuddle.client`.
- Config (projects, storage locations, rules) is read-only through the API.
- Naming: always "storage location" (URLs `storage-locations`, YAML
  `storage_locations`, Python/DB `storage_location`), never bare "location".
- Per-file work is a Prefect task; flows run tasks over many files. The API
  may call a per-file task inline (e.g. `verify_file` on register).
  Exception: index flows batch listed entries into one `index_batch` task per
  `index.batch_size` entries (a project setting, default 1).
- **Storage is generic over fsspec.** Use only the generic fsspec filesystem
  API. Never add backend-specific code (protocol names, backend packages,
  per-backend branches) outside `heliopuddle/storage/backends.py`; a unit test
  enforces this. Targets out of the box: local, SMB, SFTP, S3, Azure Blob,
  GCS and rclone remotes. hp's own `rclone://` filesystem
  (`heliopuddle/storage/rclone/`) is a backend package like s3fs and is
  exempt from that test. See `docs/design/storage.md`.
- **Code size.** No Python file over 500 lines
  (`uv run python scripts/check_file_length.py`), no function over cyclomatic
  complexity 12 (ruff `C901`), and no duplicated blocks of 8+ lines in `src`
  (`uv run pylint src`, duplicate-code only). Split by responsibility into
  well-encapsulated modules rather than suppressing the checks.
- Docs and code comments are product-neutral: do not name any particular
  mission or instrument. Domain-specific setups appear only as unnamed sample
  configs.

## Testing

- **Test first.** Write the failing test, tagged with its requirement, then
  make it pass:

  ```python
  @pytest.mark.req("CLI-01")
  def test_add_file_writes_and_registers(...): ...
  ```

- `tests/unit`: fast, no Docker (SQLite, fsspec memory/local).
- `tests/integration`: Postgres testcontainer, Azurite, Prefect test harness.
- `tests/e2e`: Docker image plus Playwright.
- `uv run lint-imports` enforces that `heliopuddle.client`, `heliopuddle.testing`,
  the user/file CLIs and `heliopuddle.server.ui` never import the DB layer.
- Run suites explicitly: `uv run pytest tests/unit`, `uv run pytest tests/integration`.
  Add `-n auto --dist loadfile` to run the unit suite on all cores (CI does).
  Unit tests must be safe under xdist: no fixed ports or shared paths.
- Coverage gate: **90%** (`--cov-fail-under=90`).
- **Test speed.** Every pytest run (local and CI) reports the slowest tests
  (`--durations` in `pyproject.toml`). Any test seen taking **longer than
  20s** during development must be tagged `@pytest.mark.slow`, so fast loops
  can skip it (`uv run pytest tests/unit -m "not slow"`), and is a target
  for optimisation. The unit suite must stay fast: a unit test that slows
  the whole suite should be made fast, moved to integration, or deleted; no
  test is better than one that makes everyone wait. Prefer driving time with
  fakes and events over real sleeps and long real leases.
- `uv run python scripts/req_matrix.py` prints the requirement → test → result
  matrix.
- Object storage is tested against **Azurite**, not MinIO.

### Running the suites in a sandbox behind a TLS-intercepting proxy

- uv needs the system CA store: `export UV_SYSTEM_CERTS=1; unset UV_NATIVE_TLS`.
- Start Docker if it is not running: `docker info || (nohup dockerd >/tmp/dockerd.log 2>&1 &)`.
- The e2e image build needs the proxy CA as a build secret, and Playwright
  needs the pre-installed browser (do not run `playwright install`):

  ```bash
  HP_CHROMIUM=/opt/pw-browsers/chromium \
  HP_E2E_BUILD_ARGS="--secret id=ca,src=/root/.ccr/ca-bundle.crt" \
  uv run pytest tests/e2e --req-report=.reports/req-e2e.json
  ```

- Never stop processes with `pkill -f <pattern>`: the pattern can match your
  own shell. Kill by PID.

## Dependencies

- Keep the **lockfile** on the latest releases. Before every milestone and
  before opening any large PR: `uv lock --upgrade`, then
  `uv run python scripts/check_deps.py`; fix or document (in that script,
  with the reason) anything behind.
- Requirements in `pyproject.toml` stay loose: add a lower bound only where
  heliopuddle or a dependency needs a newer release, or to exclude a release
  with a known security issue. Do not raise bounds just to track releases.
- The client (core dependencies and the `cli` extra) is as loose as possible
  so it installs next to other projects' pins (DEP-18): its floors are old
  and tested. Raise a floor only when the client needs it, and keep
  `uv run --no-project python scripts/client_floor.py` passing (it runs
  `tests/client` at exactly the floors). `tests/client` must not import
  server code.
- fsspec backend packages are optional extras (`heliopuddle[s3,azure,gcs,
  sftp,smb,all]`), not core dependencies.

## Commits

- Small, descriptive commits. Do not mention AI agents or assistants in commit
  messages.
- Run `uv run ruff check .`, `uv run pyright`, `uv run lint-imports`,
  `uv run python scripts/check_file_length.py`, `uv run pylint src` and the
  unit tests before pushing.

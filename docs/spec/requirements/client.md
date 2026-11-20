# Client requirements (`CLI`)

From M5A the client authenticates with a token, a username and password, or a
client id and secret ([AUTH-15](auth.md)); `user=` and `user_header` apply only
in `header` mode.
In `local` and `oidc` modes, backend connection details require
`hp-client-storage-read-write` on the project (or `hp-admin`). `add_file`,
`open` and `download` use direct fsspec access when permitted; otherwise they
transfer bytes through the API under writer/reader access without exposing
backend credentials. Metadata and run methods retain their roles.

**CLI-01 (M1)** `HelioPuddleClient(api_url, project, user=None, *,
user_header="X-Forwarded-User", http_client=None, timeout=300.0)` is a sync
client on `httpx.Client` (base URL `api_url`, e.g. `http://host:8080/api/v1`);
`http_client` accepts a pre-built `httpx.Client` instead (tests pass a
`fastapi.testclient.TestClient`). `project` may be `None` for project-agnostic
calls (`health()`, `list_projects()`, as used by the UI); project-scoped
methods then raise `ValueError`.
`add_file(local_path, path, storage_location=None, metadata=None)` checks that
`local_path` is a file, then fetches the storage-location config (URL and
resolved storage options) from `GET /projects/{p}/storage-locations/{name}`,
defaulting to the project's first storage location (the first name in
`GET /projects/{p}`, the same default the API uses). It hashes the local file
(md5, streamed) and follows the upload protocol in
[operations](../../design/operations.md) (CLI-08): reserve, write the returned
`write_path` with fsspec, or `PUT /uploads/{token}/content` for API-based
transfers, then complete. It returns a `FileRecord` (id, path,
size, hash, state, created_by, storage-location states). Because verification
is inline, the returned state is already `available` or `unknown`.
`get_file(id)` and `list_files(limit, offset)` return records;
`list_projects()`, `list_storage_locations()`, `get_storage_location(name=None)`
and `health()` (a `503` is returned, not raised) wrap the read endpoints. The
`user` argument is sent in the configured user header. A `404` raises
`NotFoundError`; any other `4xx`/`5xx` raises `HelioPuddleError(status_code,
detail)`.
*Verify:* unit test with the API in-process (`TestClient(create_api(ctx))` as
`http_client`) and a `file://` tmp storage location; integration test against
`server start`.

**CLI-04 (M1)** The client never touches the DB. It uses only HTTP to the API and
fsspec to the store. An import-linter contract forbids `heliopuddle.client`
importing `heliopuddle.server.database`, `heliopuddle.server.models` or
`sqlalchemy`. The contract runs as its own CI job (and in pre-commit), and a
violation fails CI.
*Verify:* `lint-imports` in CI; unit test that runs `add_file` in a separate
process, with database imports blocked, against the API served over HTTP; and
a unit test that importing `heliopuddle.client` loads no database module.

**CLI-07 (M3)** `trigger_index(storage_location=None, mode="full") -> RunInfo`
(flow run id and URL, and the deployment name) submits
`POST /projects/{p}/runs {kind: "index", ...}`. `accept_file(path_or_id,
storage_location=None) -> FileRecord` resolves a path to its live file, then
calls `POST /files/{id}/accept`; a `409` (copy not `unknown`) raises
`HelioPuddleError` with `status_code` 409.
*Verify:* unit tests against the in-process API (`trigger_index`, with run
submission mocked) and against a mock transport (`accept_file`).

**CLI-08 (M3)** The client follows the upload protocol ([operations](../../design/operations.md)): reserve
(`POST /projects/{p}/uploads`), write the returned `write_path`, renew during
long transfers, complete; it never writes before owning the path. While
writing, a background thread renews (`POST /uploads/{token}/renew`) every
third of the lease (the deadline minus the server's `Date`), at least 1 s
apart; a refused renew (`404`/`409`), or a deadline that passes while renews
fail, fails the write, which aborts without committing. Any failure after the
reservation (write, lost reservation, complete) releases it
(`DELETE /uploads/{token}`) and re-raises. A `409` with a `Retry-After`
header (the path is owned by another operation) raises `BusyError` (a
`HelioPuddleError` with `retry_after` in seconds, from delay seconds or an
HTTP date); nothing is written. Operations by id (`open`, `download`,
`accept_file`) fetch the record and raise `NotFoundError` if it belongs to
another project, before touching storage or calling accept; they need a
project (`ValueError` without one).
*Verify:* unit tests against a fake server (`httpx.MockTransport`): protocol
order with a spy; `unchanged` writes nothing; an overwrite writes only the
staging path; conflict and busy write nothing; write and complete failures
abort; a slow transfer is renewed; a lost reservation aborts the write and
commits nothing; cross-project ids rejected with no storage or accept call.

M4 amendment (R19): completion retries `503` with `Retry-After` at most three
times (four attempts total), keeping the lease renewed during delay-seconds
or HTTP-date waits, before aborting and raising.
Other errors are not retried.
*Verify:* retry-then-success, retry exhaustion and non-retryable errors.

**CLI-09 (M4)** HTTP-only methods `list_rules()`, `get_rule(name)`,
`run_rule(name, mode, dry_run)`, `run_once(spec)`, `enable_rule(name)`,
`disable_rule(name)`, `trigger_rule(name)` and
`set_rules_ignored(path_or_id, ignored)` expose the rule control surface.
Reads and enable/disable return `RuleRead` records, trigger returns
`RuleTriggerResult`, and both run methods return `RunInfo`. `run_once` accepts
a complete dict or `RuleSpec`. Ignore resolves paths and checks numeric IDs
against the bound project, returning the updated `FileRecord`.
`find` and `find_all` also accept `ignored_from_all_rules=None|True|False`.
Completion retries follow the CLI-08 amendment.

**CLI-10 (M5)** `reconcile(verify=False, rules=None) -> RunInfo` submits
`POST /projects/{p}/runs {kind: "reconcile", ...}`; a `409` (a reconcile is
already unfinished) or `422` raises `HelioPuddleError` with that status.
*Verify:* unit tests against the in-process API with run submission mocked.
*Verify:* in-process API and mock-transport tests.

**CLI-02 (M2, amended M6A)** `add_file(local_path, path, storage_location=None,
metadata=None, *, overwrite=False, content_date=None, version_number=None,
version_text=None)` hashes the local file, then reserves the path (CLI-08),
passing `content_date`, `metadata`, `overwrite` and the version override
(API-20). Giving both `version_number` and `version_text` raises `ValueError`
before any request. If a live file at `path` has the same hash and its copy in the
target storage location is `available`, the reservation answers `unchanged`:
the client makes no write to the store and no complete call, and returns the
existing record (`metadata` and `content_date` are then ignored). A same-hash
file with no `available` copy there is written and completed again, which
re-verifies it. `check(path, hash)` (`POST /projects/{p}/files/check`) stays
available but `add_file` does not use it.
*Verify:* spy on `StorageLocation.write` and on the complete call; the store
file's mtime is unchanged.

**CLI-03 (M2)** Queries and changes, all through the API:
`find(path=None, latest_only=False, state=None, storage_location=None,
content_date_from=None, content_date_to=None, include_deleted=False,
series_id=None, limit=100, offset=0, order="-created_at") -> FileList` (one
page of `GET /projects/{p}/files`; `path` is a glob);
`find_all(...)` (the same filters and `order`, plus `page_size=500`) iterates
every page lazily; `list_versions(path_or_series_id, include_deleted=False)
-> SeriesVersions` (newest version first); `latest(path) -> FileRead` (the
latest live version in the series containing `path`; `NotFoundError` if
none); `get_file_by_path(path)`; `remove_file(path_or_id) -> FileRead` (a path
is resolved to its live file, then `DELETE /files/{id}`; the final record has
a `deleting` copy if a store delete failed); and `update_metadata(file_id,
patch, replace=False) -> FileRead`. For `list_versions` and `latest`, a path
with no live file (for example a removed version) still identifies its series
through its deleted rows.
*Verify:* golden set of versioned files against the in-process API; removing
the latest version makes the previous one latest.

**CLI-05 (M2)** `add_file(..., overwrite=False)`: when the reservation is
refused for a conflicting hash it raises `ConflictError` (a
`HelioPuddleError` with `status_code` 409 and the existing record as
`.file`) without touching the store. With `overwrite=True` it reserves with
`overwrite=true`, writes the staging path the server returns, completes, and
the returned record has the new hash.
*Verify:* unit tests: the store is untouched on conflict; overwrite replaces
content and hash.

**CLI-06 (M2)** `open(path_or_id) -> BinaryIO` returns a binary file object
read directly from the store; `download(path_or_id, dest) -> Path` streams to
a temp file next to `dest` (an existing directory gets the file's name),
checks the md5 against the record, then moves it into place. On a mismatch it
removes the temp file, leaves `dest` untouched and raises `IntegrityError`
(with `.file`, `.expected` and `.actual`; not a `HelioPuddleError`, since the
API did not fail). Both use the storage-location config from the API and an
`available` copy, or during an overwrite the `overwriting` copy (which still
holds the original); with neither they raise `FileNotFoundError`.
*Verify:* unit tests, including tampered store content.

**CLI-11 (M6)** `search(q, projects=None, *, latest_only=False,
include_deleted=False, limit=20, offset=0) -> SearchResults` calls
`GET /search/files` (it is not scoped to the client's project);
`project_summary(project=None) -> ProjectSummary` calls
`GET /projects/{p}/summary` (default: the client's project); `find(state=...)`
accepts one state or a list of states, and `find`/`find_all` take
`deleted_only`. Errors raise `HelioPuddleError` with
the status.
*Verify:* unit tests against the in-process API.

**CLI-12 (M6)** `preview(path_or_id) -> FilePreview` calls
`GET /files/{id}/preview` (a path is resolved to its live file first).
*Verify:* unit tests against the in-process API.

**CLI-13 (M6, amended M6A)** `register_file(path, hash, size,
storage_location=None, metadata=None, *, content_date=None,
version_number=None, version_text=None) -> FileRecord` calls
`POST /projects/{p}/files` (API-01) for a file another tool has already
written to the store; the client reads and writes no content. The version
override follows API-20 and API-22 (both given: `ValueError` before any
request; a different version for a live path: `ConflictError`). A new path
returns the new record, the same hash the existing record (`metadata` and
`content_date` are then ignored); the record is verified inline (`available`,
or `unknown` when the object does not match). A different hash raises
`ConflictError` with the existing record, a busy path `BusyError`, an unknown
project or storage location `NotFoundError`.
*Verify:* unit tests against the in-process API.

**CLI-14 (M6, amended M6A)** **Test harness.** `heliopuddle.testing.FakeHelioPuddleClient`
is an in-memory drop-in for the client's file methods (`register_file`,
`add_file`, `get_file`, `get_file_by_path`, `check`, `find`, `find_all`,
`list_files`, `latest`, `remove_file`, `update_metadata`, `set_content_date`, `accept_file`,
`list_storage_locations`, `health`, `close`) with the same signatures, records and errors as the real
client and API. It needs only the core install. With a `store` directory it
verifies registrations against it, writes uploads to it and deletes removed
objects from it, and records each copy's last observation (size, mtime, hash)
as the server does: registering the same hash again re-verifies a copy that
is not `available` and adds a copy in another storage location; a delete
leaves an object that changed or was never observed in the store as
`unknown` (the file stays live), and a failed store delete leaves the copy
`deleting`. `health()` returns a `HealthResponse` with every part `ok`. Test
helpers: `calls`, `methods_called()`, `seed()` (checks the path, storage
location and live path before writing), `inject_busy()`, `inject_error()`,
`record_at()`, `live_paths()`, `reset_calls()`. A pytest plugin (entry point
`pytest11`) provides the fixtures `heliopuddle_fake` and `heliopuddle_store`.
Version overrides are stored and checked as by the server (both given,
a different version for a live path); without an override a file is
unversioned. Simplifications: one project, every path is its own series
(latest when it has an `available` copy), verification is immediate, only the first storage
location has a store (copies elsewhere are trusted), nothing is busy unless
injected.
*Verify:* contract scenarios run against the fake and against the real
client with the in-process API (unit) and with real `server start` processes
on SQLite and Postgres (integration); a test compares every shared method's
signature with the client's; unit tests of the helpers; the harness imports
with every server-only package blocked.

**CLI-15 (M6, amended M6A)** **File CLI.** `heliopuddle file add LOCAL [PATH]
[--recursive] [--overwrite] [--metadata JSON] [--content-date]
[--storage-location] [--version-number N | --version-text TEXT]`,
`file register PATH --hash --size [--metadata] [--content-date]
[--version-number N | --version-text TEXT]`,
`file list [GLOB] [--latest] [--deleted] [--limit]`, `file show`,
`file update`, `file download [--to DEST]` and `file remove [--yes]`.
`show`, `update`, `download` and `remove` choose files by id (positional
arguments; a number is always an id) or with `--path GLOB` (live files whose
path matches; a plain path matches that file) and `--name GLOB` (live files
whose file name, in any folder, matches); `show` and `download` need exactly
one match, `update` and `remove` take one or more ids and globs, and a glob
that matches nothing is an error. `file update` sets metadata keys
(`--set KEY=VALUE`, the value parsed as JSON if it can be, else a string),
removes them (`--unset KEY`), merges `--metadata JSON` or, with
`--replace-metadata`, replaces the metadata, and sets (`--content-date`) or
clears (`--clear-content-date`, API-19) the content date; a version is set
only by `add` and `register` (one file; giving both flags, a version with
a directory, or a value outside the API's range (API-20) exits 2 before any
request) and never changed (API-22). `file show` prints the
version (`v12` for a number, the text as is). `remove` lists the chosen files and
asks first (after reading any credentials, so `--password-stdin` takes the
first line of stdin); when a file is not fully deleted it exits 1 and says,
per copy state, whether the delete failed (`deleting`, which the index
retries) or changed content was kept (`unknown`, to review). Every command
takes `--project` (`HELIOPUDDLE_PROJECT`, default `default`), `--api-url` and
the same credentials as `heliopuddle user`, plus `--user` for `header` mode;
`file list` shows one state column per storage location (the active ones in
config order, plus an inactive one that still holds a listed copy), with `-`
where the file has no copy or its copy is deleted (a moved file shows `-` in
its source), and marks a deleted file `(deleted)` after its path; `add`,
`register`, `update` and `remove` print `ID`, the copies as
`location=state[,...]` (`-` when none is left) and the path. `--json` prints records. Exit codes: 0 success, 1 API error, conflict or no
match, 2 invalid options. It needs only the `cli` extra (`typer`), which
`server` and `all` include.
*Verify:* CLI tests against the in-process API.

**CLI-16 (M6)** `set_content_date(path_or_id, content_date) -> FileRecord`
calls `PATCH /files/{id}/content-date` (API-19); `None` goes back to the date
in the path, or none. An unknown or deleted file, or another project's id,
raises `NotFoundError`.
*Verify:* unit tests against the in-process API.

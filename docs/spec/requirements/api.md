# API requirements (`API`)

Base path `/api/v1`. From M5A every route except the public ones requires
authentication and a role ([AUTH-07](auth.md)); the user id is the
authenticated username. The header named by `HELIOPUDDLE_USER_HEADER` (default
`X-Forwarded-User`) is used only with `HELIOPUDDLE_AUTH_MODE=header`, which is
what the requirements below describe for M1–M4.

**API-01 (M1)** `POST /projects/{p}/files {path, storage_location, hash, size,
metadata?}` validates the project, the storage location (must be active) and
the path (relative, no `..`, not `*.hp-tmp`). In one transaction under a
series row lock it upserts the series (unversioned: key = path), the `file`
and a `file_storage_location` in `pending`, then commits. It then calls the
`verify_file` task **inline** and returns `201` with the record and its final
state. `GET /projects/{p}/files` (`limit`/`offset` paging, newest first) and
`GET /files/{id}` (with per-storage-location states) return records.
`created_by` is the value of the header named by `HELIOPUDDLE_USER_HEADER`
(default `X-Forwarded-User`); a missing header gives NULL. Unknown project or
storage location → `404` (also for an inactive storage location); invalid body
or path → `422`. In M1 registering an existing live path updated that file in
place; from M2 the outcomes at a live path follow API-07.
*Verify:* unit tests with a custom header name and with no header; verify runs
under `prefect_test_harness`.

**API-03 (M1)** `GET /projects/{p}/storage-locations` lists storage locations (name,
url, accept_external_changes, active, create_root as configured). `GET /projects/{p}/storage-locations/{name}`
also returns `storage_options` (empty for an inactive storage location, which
the list still includes with `active: false`). `${VAR}` and `${VAR:-default}` references in
a storage location's `url` and `storage_options` values are resolved from the
server environment when config is loaded; an unset variable with no default
fails config loading at startup (DEP-05).
From M4 both views also expose `quota` as bytes, or null when unset.
From M5A, backend connection details require `hp-client-storage-read-write`
(global or scoped to the project; `hp-admin` also suffices) in `local`
and `oidc` modes. The list still requires reader access but returns `url: ""`
without that role on the project (active and inactive alike), so URL-embedded credentials cannot
bypass the role boundary. Trusted-proxy `header` mode is unchanged.
*Verify:* unit test with an env var in `storage_options`.

**API-05 (M1)** Projects, storage locations and rules are read-only through the API:
config routes expose only `GET`, and no endpoint creates, changes or deletes
config. M4 rule enable/disable changes runtime state, not config; a one-off
run is unsaved.
*Verify:* route-table test.

**API-06 (M1)** `GET /api/v1/health` returns `200` with `{status, database, prefect}`
when the DB and Prefect are reachable, else `503` with `error` for the failing part (no exception text or
URL: the route is unauthenticated; details go to the server log). Prefect is checked with
`GET {PREFECT_API_URL}/health` (short timeout); an unset `PREFECT_API_URL` counts
as a failure.
`GET /api/v1/version` returns the package version.
*Verify:* unit tests (Prefect reachable and not).

**API-02 (M2, amended M6A)** The series and version are derived by the
server from the path using the project's `versioning` rules: first match wins;
a project without `versioning` gets one default `number` rule,
`_[vV](?P<version>\d{1,5})\.[^./]+$`, and a project that sets `versioning`
replaces it; no match means unversioned. A `number` rule stores
`file.version_number` from the `version` group, else the whole match (digits,
leading zeros ignored). A `text` rule stores `file.version_text` from its
`version_text` format template over the named groups (as strings; a group that
did not take part is `""`), else from the `version` group or the whole match.
At most one of the two is set. Series keys follow
[versioning](../../design/versioning.md): the version parts (the `version`
group, the template's groups, or the whole match) and any `ignore_in_series`
groups are replaced by placeholders; `date` is never a version part, and an
extension splits series unless ignored. Within a series exactly one live
file is `is_latest`: the highest `version_number`, or the highest
`version_text` compared byte-wise, by the series' kind (API-21); unversioned
rows rank lowest; ties go to the newest `created_at`, then the highest id. It
is recomputed in the same transaction as every add, overwrite and remove,
under the series row lock, and `series.latest_file_id` is kept in step.
`content_date` comes from the register request, else from a `date` group in
the matching rule.
*Verify:* table-driven unit tests over the examples in
[versioning](../../design/versioning.md); add/remove sequences checking
`is_latest` and `latest_file_id` for number and text series.

**API-07 (M2)** `POST /projects/{p}/files/check {path, hash}` returns `{exists,
hash_matches, file}`. `GET /projects/{p}/files/by-path?path=` returns the live
file or `404`. A register with the same hash at a live path returns `200` with
the existing record (re-verifying only if the copy is not `available`). A
different hash returns `409` with the existing record in the body. An
overwrite updates the same row (new hash and size, `updated_by` set,
`created_by` kept); from M3 it goes through the upload protocol
(`POST /projects/{p}/uploads` with `overwrite`, STO-09/STO-10), and a register
with `overwrite=true` and a different hash returns `409` pointing there.
*Verify:* unit tests.

**API-08 (M2)** `DELETE /files/{id}` removes the file inline by calling the
`delete_file` task (SVC-07) and returns the final record; deleting an
already-deleted file returns `404` (from M3: a busy copy `409`, an unexpected
error `500`, SVC-15). `PATCH /files/{id}/metadata` takes a JSON
object: a shallow merge by default (a `null` value removes a key), or a full
replacement with `?replace=true`; it sets `updated_by`.
*Verify:* unit tests, including deleting the latest version so the previous
one becomes latest.

**API-09 (M2)** `GET /projects/{p}/files` filters: `path` (glob with `*`,
`**`, `?`; narrowed in SQL with a `LIKE` prefix, then matched exactly before
paging), `series_id`, `latest_only`, `state`, `storage_location`,
`content_date_from`/`content_date_to`, `include_deleted` (default false),
`deleted_only` (from M6: only deleted files),
`limit` (1–1000, default 100)/`offset`, and `order` (`-created_at` default,
or `path`; ties by id). `state` matches a copy in that state, and with
`storage_location` a copy in that state in that storage location;
`storage_location` alone matches a copy there that is not `deleted`. The
content date range is inclusive at both ends. `total` counts after
filtering. From M6 `state` may be repeated and then matches a copy in any of
the given states (in `storage_location` when that is given too).
`GET /series/{id}/versions` returns the live versions
newest version first, and with `include_deleted` also the deleted ones.
*Verify:* golden-dataset unit tests, one per filter and in combination; paging
totals.

**API-10 (M3)** `POST /projects/{p}/runs {kind: "index", storage_location?,
mode: "full" | "incremental"}` submits the storage location's index deployment
run without waiting and returns `202 {flow_run_id, flow_run_url}`. The default
storage location is the project's first. Unknown project or storage location →
`404`; inactive → `409`; bad kind or mode → `422`; the deployment not
registered or Prefect unreachable → `503`. `GET
/projects/{p}/storage-locations[/{name}]` also returns `last_full_index_at` and
`last_incremental_index_at`.
*Verify:* unit tests with run submission mocked; integration test with a real
Prefect server.

**API-11 (M3)** `POST /files/{id}/accept {storage_location?}` accepts an
`unknown` copy as it is now: the copy is re-hashed, the file's hash and size
are set from it, the copy becomes `available`, `updated_by` is set and
`is_latest` recomputed. The default storage location is the file's only
`unknown` copy (ambiguous → `422`). Unknown or deleted file → `404`; copy not
`unknown` → `409`.
*Verify:* unit tests, including that accepting makes the file eligible for
latest.

**API-12 (M3)** Latest eligibility (amends API-02): `is_latest` and
`series.latest_file_id` consider only live files that have at least one copy
in `pending` or `available`. A series with no eligible file has no latest. It
is recomputed whenever a copy state change can alter the result (verify,
index, accept, delete).
*Verify:* table tests over copy-state combinations, including a series with no
eligible file.

**API-13 (M3)** Series keys distinguish generated keys from literal paths
(`v:` and `p:` prefixes), so
`x_v1.dat` and an unversioned `x_v{v}.dat` never share a series.
*Verify:* unit test; migration test.

**API-04 (M4)** `POST /projects/{p}/runs` accepts the existing index request,
`{kind: rule, rule, mode?, dry_run?}` or `{kind: rule_once, spec}` and returns
`202 {flow_run_id, flow_run_url, deployment}`. One-off specs validate like
config (`422`) and run through `rule-once` in full mode as exactly one batch,
without retrigger. Configured rules cannot be overridden except mode/dry run.
Disabled configured rules return `409 disabled`. A configured rule that
already has an unfinished (scheduled, pending or running) run returns `409`
naming that run (future runs the Prefect scheduler created for the cron are
not unfinished) and submits nothing: that run re-selects after every batch
and continues while work remains.
From M5, `{kind: reconcile, verify?: bool, rules?: [names]}` submits the
project's reconcile deployment and returns the same `202` body; an unfinished
reconcile run returns `409` naming that run; an unknown rule name returns
`422`.
*Verify:* unit (mocked) and integration (real Prefect) tests.

**API-14 (M4)** `GET /projects/{p}/rules[/{name}]` returns validated config
with defaults, effective enabled state, deployment name and watermark.
Optional `deployment_id` and `deployment_url` link to the actual registered
Prefect deployment by UUID; unavailable links are null without hiding config.
`POST .../rules/{name}/enable`, `/disable` and `/trigger` exist; trigger
returns `{started, flow_run_id?}`.
*Verify:* endpoint tests, including Prefect failure and disabled state.

**API-15 (M4)** `PATCH /files/{id}/rules-ignore {ignored}` sets
`ignored_from_all_rules` and records `updated_by`. Un-ignore updates
`updated_at`; file records expose the flag, and file queries accept the
optional `ignored_from_all_rules` boolean filter.
Indexing still tracks ignored files.
*Verify:* API/client query tests, incremental un-ignore selection.

**API-16 (M6)** `GET /search/files?q=&project=&latest_only=&include_deleted=&limit=&offset=`
searches file paths across projects. Any authenticated caller may search; only
projects the caller holds a role on are searched (as `GET /projects`), and
`project` (repeatable) narrows them. A named project that is unknown or that
the caller holds no role on gives the same `404`. `q` (2–200 characters) is
split on whitespace; every word must occur in the path, case-insensitively, as
literal text (`%`, `_`, `*` and `?` are not wildcards). `latest_only` and
`include_deleted` mean the same as in API-09. `limit` is 1–100 (default 20).
Results are ranked: file name equal to a word, then name starting with a word,
then name containing a word, then a match only in the folder path; ties
newest first, then by id. The response is `{items, total, limit, offset,
projects: {name: count}}`, where `projects` counts the matches per searched
project (projects with none are left out). Bad parameters give `422`.
*Verify:* unit tests of the role and project-scope matrix (global, scoped,
admin, header mode), ranking, multi-word matching, literal wildcards, paging
and counts; a Postgres integration test that a page over 200k files across
three projects returns within 500 ms.

**API-17 (M6)** `GET /projects/{p}/summary` (needs read on the project)
returns `{project, files, latest_files, bytes, deleted_files,
storage_locations: [{name, active, files, bytes, states: {state: count},
no_copy}], views: {needs_attention, in_progress, ignored}}`. `files`,
`latest_files` and `bytes` count live files; `deleted_files` counts deleted
ones. Per storage location (in config order), `states` counts copies of live
files in each copy state (every state present, zero included), `files`
counts live files with a copy there that is not `deleted`, `bytes` sums
their sizes, and `no_copy` counts live files without such a copy. Views
count live files: `needs_attention` with a copy `unknown` or `missing`,
`in_progress` with a copy `pending`, `overwriting` or `deleting`, and
`ignored` with `ignored_from_all_rules`. Each figure comes from aggregate
SQL; nothing is cached or stored.
*Verify:* golden-dataset unit tests; Postgres integration test.

**API-18 (M6)** `GET /files/{id}/preview` (needs read on the file's project)
returns a `FilePreview` of a live file's content, read on the server from the
first readable copy (`available`, else `overwriting`). The format comes from
the file extension, case-insensitively:

| Extension | Preview |
|---|---|
| `.csv`, `.tsv` | a table: header and the first 100 rows (delimiter sniffed) |
| `.json` | a top-level array: a table of its first 100 items (objects become columns); otherwise the pretty-printed JSON of the first 100 top-level entries |
| `.jsonl`, `.ndjson` | a table of the first 100 lines |
| `.txt`, `.log` | the first 100 lines |
| `.md`, `.markdown` | the Markdown source, at most 256 KiB |
| `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.svg` | kind `image` with its media type |
| `.parquet` | schema types, key-value metadata, total rows and the first 100 rows |
| `.cdf` | global attributes, then one table per group of variables with the same record count (read with crump), each with its variables' attributes, total records and the first 100 records; array variables expand to one column per element, named from their label attributes; variables that cannot be read are named in `message` and the rest are still shown |

Any other extension gives kind `none`. Reading is bounded: text formats read
at most 4 MiB from the start of the file; Parquet reads the footer and only
the first rows; CDF reads only the first 100 records of each variable. A CDF
on a storage location without local file paths is first copied to a temporary
file, only if it is at most `HELIOPUDDLE_PREVIEW_MAX_BYTES` (default 1GB),
else kind `too_large`. Tables show at most 100 columns (`hidden_columns`
counts the rest) and cells at most 1,000 characters. A file that cannot be
parsed as its format gives `200` with kind `error` and a message, never `500`.
A deleted file or one with no readable copy gives `404`; a storage read
failure `503`. Previews are cached in memory per copy (keyed on file,
storage location, size and modified time) for the server process.
*Verify:* unit tests per format, including truncated and damaged files and
the reading bounds (a 200 MB CSV and a CDF with a million records preview
without reading them whole); a test that a CDF too large to copy gives
`too_large`.

**API-19 (M6)** `PATCH /files/{id}/content-date {content_date}` (writer on the
file's project) sets a live file's content date; a naive time is UTC, and
`null` goes back to the date the project's version rules take from the path,
or to none. It sets `updated_by` and `updated_at` and notifies the rules.
Unknown or deleted file → `404`; a missing or invalid `content_date` → `422`.
The version is not changed: it is set only when the file is created (API-20,
API-22).
*Verify:* endpoint unit tests; the auth enforcement table.

**API-20 (M6A)** **Version overrides.** `POST /projects/{p}/files` and the
upload reserve accept an optional `version_number` (integer, 0 to 2^63−1) or
`version_text` (1–255 characters), but not both (`422`). For a new file the
override replaces the version derived from the rules; the series key is still
derived from the path (the literal path when no rule matches). Without an
override the rules apply. File records return `version_number` and
`version_text`; `GET /series/{id}/versions` also returns the series'
`version_type`.
*Verify:* endpoint unit tests: number and text overrides, both given, out of
range, a path no rule matches, no override.

**API-21 (M6A)** **One kind per series.** `series.version_type` (`number`,
`text`, or NULL while the series has no versioned file) is set by the first
versioned file of the series and never changes. A register or reserve whose
version (derived or overridden) is of the other kind gets `422` naming the
series' kind; the indexer records such a file as unversioned in its
literal-path series with an `unknown` copy and a logged reason. Unversioned
files may share any series and rank lowest.
*Verify:* unit tests for register, reserve and index.

**API-22 (M6A)** **Versions are immutable.** A file's version is fixed when it
is created. Re-registering, re-reserving or overwriting a live path with no
override, or with an override equal to the stored version, keeps the version;
a different override gets `409`. No endpoint changes a version.
*Verify:* unit tests for register, reserve with `overwrite`, and metadata and
content-date updates.

**API-23 (M6A)** **Upload size cap.** `HELIOPUDDLE_MAX_UPLOAD_SIZE` (a byte count or
a size such as `2GiB`; default `1GiB`, must be above zero) is the largest size a
reservation (`POST /projects/{p}/uploads`) or registration (`POST
/projects/{p}/files`) may declare: above it the answer is `413` naming the limit.
`PUT /uploads/{token}/content` already refuses more bytes than were reserved
(`413`).
*Verify:* unit tests of both routes, the setting and the content cap.

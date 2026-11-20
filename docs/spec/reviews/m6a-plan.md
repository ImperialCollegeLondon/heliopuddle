# M6A plan (approved): number and text versions

## Context

Today a file has `version`, `version_major` and `version_minor` columns, and
each series has a `latest_by` (`major_minor` | `minor`). Two default rules are
always appended to the project's rules. This is rigid: a version can only be an
integer or a pair of integers, and the client cannot supply a version itself.

The new model:

- a file stores **either** `version_number` (BIGINT) **or** `version_text`
  (text), never both;
- rules extract the version with a regex, and text rules normalise it with a
  Python format template;
- API, client and CLI can override the version, but only when the file is
  created.

There are no installed users, so this is a breaking rewrite with no
compatibility shims. Branch: `feat/better-versions`.

### Is it a good idea? Yes, with these changes to the original request

1. **No `COALESCE(version_number, version_text)`.** It mixes an int and a text
   column. Postgres rejects it unless the int is cast, and once cast `10` sorts
   before `9`. A COALESCE expression also can't use the requested indexes.
   Decision (owner): **a series never mixes kinds.**
   - `series.version_type` (`number` | `text` | NULL) is set by the first
     versioned file.
   - A later file of the other kind is rejected with `422`; the indexer records
     it as `unknown` with a reason.
   - `ORDER BY` uses only that series' column. This gives the "number-only sort
     is faster" goal for every series. A config-level shortcut ("no text rules
     in the project") would be unsafe, because overrides and earlier configs can
     still produce text rows.
2. **The default rule must not match dates or frame numbers.** The proposed
   `(?P<version>\d{1,10})\.[^./]+$` would fold `a_20250101.csv` and
   `a_20250102.csv` into one series. Decision (owner): the default is
   `_[vV](?P<version>\d{1,5})\.[^./]+$`.
3. **Text sorts byte-wise.** Postgres' default collation can ignore
   punctuation, so `version_text` uses `COLLATE "C"`. SQLite is binary already.
   Users zero-pad numbers in the template (`{major:0>3}`) so lexical order is
   version order. The docs and README say so.
4. **Indexes are per series.** Every version query filters by `series_id`, so
   the two requested indexes are composite:
   - `ix_file_series_version_number (series_id, version_number DESC)`
   - `ix_file_series_version_text (series_id, version_text DESC)`

   An index on `version_number DESC` alone would not help the latest-file
   lookup.
5. **`latest_by: minor` is removed** (owner). Latest is always the highest
   version.

## New config shape

```yaml
versioning:                # first match wins; replaces the default when set
  - paths: ["**"]
    version_type: number   # default
    extraction_pattern: '_[vV](?P<version>\d{1,5})\.[^./]+$'
  - paths: ["calibration/**"]
    version_type: text
    extraction_pattern: '_v(?P<major>\d{1,3})\.(?P<minor>\d{1,4})\.(?P<ext>[^./]+)$'
    version_text: '{major:0>3}.{minor:0>4}'
    ignore_in_series: [ext]   # optional: .csv and .json versions share one series
```

### Default rules

The `versioning` field defaults to the single rule above. A project that sets
`versioning` replaces the default; `versioning: []` turns versioning off. This
replaces the current "always append `DEFAULT_RULES`" behaviour.

### Extracting the version

Matching is `re.search` on the file name.

- **`number`**: take the `version` group if the pattern has one, else the whole
  match. The value must be ASCII digits, otherwise it raises
  `InvalidVersionError` (register gives `422`, the indexer records `unknown`).
  It is stored as an int.
- **`text`**: if `version_text` is set, it is filled in with
  `str.format(**groups)`. A group that didn't take part in the match is `""`.
  Without a template, the value is the `version` group, or else the whole match.
  An empty result is an `InvalidVersionError`. The maximum length is 255.
- A `date` group still feeds `content_date`.

### Series key

The series key is the path with the **version parts** replaced by
placeholders, still prefixed `v:`. Unmatched paths are `p:` plus the path, as
today. The version parts are:

- the `version` group, or the groups used by the `version_text` template, or
  the whole match if there are neither;
- plus any groups listed in `ignore_in_series`.

`date` is never a version part. Config validation rejects `date` in
`version_text` or in `ignore_in_series`.

**Extensions:**

- **Default (different series):** a different extension means a different
  series, because `ext`, like any other group not in the list above, stays
  literal in the key.
- **Shared series:** a user opts in with `ignore_in_series: [ext]`.

Placeholders are `{v}` for a `version` group or the whole match, and
`{<name>}` for other named groups.

### Validation (DEP-06)

An invalid rule stops startup, naming the file, rule index and field. Checks:

- `extraction_pattern` compiles;
- `version_type` is `number` or `text`;
- `version_text` is allowed only for `text`. Its fields must be named groups of
  the pattern (not `date`). A trial format with every group set to `"0"` must
  succeed, which catches format specs such as `:d` on strings;
- `ignore_in_series` names existing groups (not `date`);
- `paths` is non-empty.

The old `pattern` and `latest_by` keys fail validation, because the model uses
`extra="forbid"`.

## Overrides (API, client, CLI)

- **Register and upload reserve** take optional `version_number: int (>= 0)` or
  `version_text: str (1–255)`.
  - A pydantic model validator rejects both at once (`422`).
  - An override replaces the rule-derived version. The series key still comes
    from the rule, or is the literal path if no rule matches.
  - An override of the wrong kind for an existing series gives `422`.
- **The version is immutable.** Re-registering a live path whose override
  differs from the stored version gives `409`. A matching override, or no
  override, is a no-op, as today. `PATCH content-date` and `file update` never
  touch the version.
- **Client:**
  - `add_file(...)` and `register_file(...)` gain the keyword arguments
    `version_number=None` and `version_text=None`; passing both raises
    `ValueError` before any HTTP call.
  - `FakeHelioPuddleClient` stores the override and enforces the same rules.
- **CLI:** `file add` gains `--version-number N` and `--version-text TEXT` as a
  mutually exclusive group. A directory add with `--recursive` rejects them.
- **Responses:**
  - `FileRead` returns `version_number` and `version_text` (no `version`,
    `version_major` or `version_minor`).
  - The series-versions response includes `version_type`.
  - The UI shows `v12` for a number and the text as is.

## Database: new migration `0008_version_number_text.py`

- `file`:
  - drop `version`, `version_major`, `version_minor`;
  - add `version_number BIGINT NULL` and `version_text VARCHAR(255) NULL`
    (`COLLATE "C"` on Postgres);
  - add `CHECK (version_number IS NULL OR version_text IS NULL)`;
  - add the two composite indexes.
- `series`: drop `latest_by`; add `version_type VARCHAR(8) NULL`.
- Data: there are no users, so there is no data conversion. Upgrade maps the
  old `version` to `version_number` and the old major/minor to NULL; downgrade
  restores the columns empty.
- Use `batch_alter_table` for SQLite, following the existing migrations.
  Update `test_search_migration.py` and the migration-chain tests.

## Code changes (representative files)

- `src/heliopuddle/config/models.py`: `VersionRule` gets `paths`,
  `version_type`, `extraction_pattern`, `version_text`, `ignore_in_series`,
  plus the validators above. Remove `LatestBy`. The default for `versioning`
  becomes the one-rule list.
- `src/heliopuddle/versioning.py`:
  - rewrite `derive()` to return
    `VersionInfo(series_key, version_number, version_text, version_type, content_date)`;
  - remove `DEFAULT_RULES` (moved to config), `sort_key` and major/minor;
  - add `apply_override(info, version_number, version_text)`.
- `src/heliopuddle/server/models/series.py`:
  - `lock_series` sets `series.version_type` the first time and raises
    `VersionKindMismatch` on a clash;
  - `_version_order(version_type)` orders by one column, with
    `COALESCE(..., -1 / '')` for unversioned rows and then
    `created_at DESC, id DESC`;
  - `version_key`, `ordering_key` and `sort_versions` follow suit.
- `src/heliopuddle/server/models/files.py` and `models/uploads.py`: take the
  override, write the new columns, and return `409` when an existing file's
  version differs.
- `server/api/files/register.py`, `server/api/uploads/reserve.py`,
  `server/api/errors.py`: new request fields, plus `422` and `409` mapping.
- `server/schemas/files.py`, `server/api/files/series_versions.py`,
  `server/ui/shared/fileinfo.py`, `cli_file.py` (show/add), `client/client.py`,
  `testing/fake.py`.
- `server/services/index.py`: indexing still calls `derive`. A kind mismatch or
  an invalid version gives `unknown` with a reason.
- `config/loader.py`: replace the sample/default config template with the new
  shape.

## Docs (committed with or before the code, per CLAUDE.md)

- **`docs/design/versioning.md`:** rewrite it. Cover:
  - rule fields and the default rule;
  - number and text extraction;
  - the format template, with a zero-padding note;
  - version parts and `ignore_in_series`, with a series-key example table that
    includes `.csv` and `.json` with and without `ignore_in_series`;
  - one kind per series;
  - overrides and immutability;
  - ordering and collation.
- **`docs/design/data-model.md`:** new columns, CHECK constraint, indexes,
  `series.version_type`.
- **`docs/design/indexing.md`:** the version-error cases.
- **Requirements:** rewrite API-02, DEP-06, API-19 (the version is set only at
  creation), CLI-02/CLI-13 (override keyword arguments), CLI-15 (CLI flags) and
  CLI-14 (fake). Add the new IDs below.
- **`README.md`:** the text-rule example from the request (`major.minor`
  zero-padded) next to the default number rule.
- **`docs/spec/features.md`:** list this change under M6 follow-ups, or as a
  small `M6A` (see the note below).

### Requirement table (in scope)

| ID | Description |
|---|---|
| API-02 (rewritten) | The server derives series and version from the path using the project's `versioning` rules, first match wins. When `versioning` is unset, the default is one number rule, `_[vV](?P<version>\d{1,5})\.[^./]+$`. No match means unversioned. A number rule stores `version_number` from the `version` group or the whole match. A text rule stores `version_text` from the `version_text` template over the named groups, or from the `version` group or the whole match. At most one of the two is set (DB CHECK). The series key replaces the version parts and any `ignore_in_series` groups with placeholders; `date` is never a version part. Within a series, exactly one eligible live file is latest: the highest version of the series' kind, with ties broken by newest `created_at`, then highest id. `content_date` comes from the request, else from a `date` group. |
| API-20 (new) | Register and upload reserve accept an optional `version_number` (int ≥ 0) or `version_text` (1–255 chars), but not both (`422`). An override replaces the rule-derived version for a new file. The series key is still rule-derived. |
| API-21 (new) | A series holds only number versions or only text versions (`series.version_type`, set by the first versioned file). A register, reserve or index of the other kind is rejected: `422` naming the series kind, or `unknown` with a reason in the indexer. |
| API-22 (new) | A file's version is fixed at creation. Re-registering a live path with an override that differs from its stored version gives `409`. No endpoint changes a version. |
| API-19 (amended) | …the version is set only at creation (API-20, API-22). |
| DB-07 (new) | `file.version_number BIGINT`, `file.version_text VARCHAR(255)` (`COLLATE "C"` on Postgres), a CHECK that at most one is non-null, indexes `(series_id, version_number DESC)` and `(series_id, version_text DESC)`, and `series.version_type`. They are added by migration 0008. |
| DEP-06 (rewritten) | `versioning` rules are validated at startup. The checks are: `extraction_pattern` compiles; `version_type` is `number` or `text`; `version_text` is only for `text`, references only named groups of the pattern (not `date`), and formats successfully on trial; `ignore_in_series` names existing groups (not `date`); `paths` is a non-empty list of globs. An invalid rule stops startup, naming the file, rule index and field. |
| CLI-02 / CLI-13 (amended) | `add_file` and `register_file` take `version_number=None` and `version_text=None`; passing both raises `ValueError` without an HTTP call. |
| CLI-14 (amended) | The fake client accepts and stores the overrides with the same both-set and immutability errors. |
| CLI-15 (amended) | `heliopuddle file add` accepts `--version-number` or `--version-text` (mutually exclusive, single file only). |

## Build (test first, delegated by REQ)

1. **Agent A** (DEP-06, API-02 derivation): config model, `versioning.py`,
   `test_versioning.py` and `test_config_loader.py` rewritten as table-driven
   tests:
   - default rule;
   - whole-match number;
   - text template with padding;
   - `ignore_in_series: [ext]`;
   - `date` exclusion;
   - invalid values.
2. **Agent B** (DB-07, API-02 ordering, API-21): migration 0008, ORM,
   `models/series.py`, `test_latest_sql.py` and `test_series*.py`, plus
   Postgres integration (`test_postgres_search.py`, collation ordering of
   `001.0002` and `001.0010`).
3. **Agent C** (API-20, API-22, API-19, CLI-02, CLI-13, CLI-14, CLI-15): API
   schemas and endpoints, client, fake, CLI, UI `fileinfo`. Also the contract
   tests that run the fake against real servers.
4. **Me:** the index service changes, the docs and README, and fixing up the
   remaining tests that use `version` or `version_major`
   (`test_files_api.py`, `test_index*.py`, `test_ui.py`,
   `test_change_key_paging.py`).

## Verification

- `uv run ruff check .`, `uv run pyright`, `uv run lint-imports`
- `uv run pytest tests/unit -n auto --dist loadfile` with coverage ≥ 90%;
  `uv run pytest tests/integration` (Postgres collation and indexes; run
  migration 0008 up and down)
- `uv run --no-project python scripts/client_floor.py` (client signature
  change)
- `uv run python scripts/req_matrix.py` shows API-02, API-19, API-20, API-21,
  API-22, DB-07, DEP-06 and CLI-02/13/14/15 passing.
- End-to-end checks:
  - `uv run heliopuddle server start` with no config;
  - `file add x_v3.csv`, then `x_v12.csv`: the latest is 12;
  - `file add y.csv --version-text 001.0002`: stored as text;
  - adding with both flags is rejected;
  - a text-rule project indexes `a_v1.2.csv` and `a_v1.10.csv` with
    `{major:0>3}.{minor:0>4}`: the latest is `001.0010`;
  - the UI file page shows the version;
  - `docker run` the image;
  - the e2e suite.

## Process note

M6 is merged to `main`; `feat/better-versions` restarts from it. The milestone
is M6A (between M6 and M7). `file register` gets the same version flags as
`file add`.

## Addendum (approved): rule ages

Added to M6A on the same PR by the owner's decision.

- `match.age` replaces `older_than`, `age_by`, `content_date_after` and
  `content_date_before` (removed, no shim). Fields `modified`, `created`,
  `content_date` and `arrived`, each with `older_than` and/or `younger_than`,
  a duration or an absolute ISO date/time; all listed criteria must hold.
  `is_latest` keeps `null`/`true`/`false`.
- `arrived` (owner's choice of name) is a new `file_storage_location.arrived_at`
  (migration `0009`, backfilled from `updated_at`): set on first arrival
  (upload, register, index add) and on every copy **and** move rule transfer
  into the location (owner's choice), giving a cooling-off period.
- Durations everywhere gain `w`, `mo` and `y` units and long forms; months and
  years are fixed 30 and 365 days (owner's choice).
- Incremental runs pick up crossings of any relative `older_than` bound.
- Requirements: SVC-04, SVC-05, DEP-13, UI-04 amended; SVC-29, DB-08 and
  DEP-19 new. Design: [rules](../../design/rules.md#age-criteria).

# Database requirements (`DB`)

**DB-01 (M1)** On start, `alembic upgrade head` creates the schema on SQLite:
`project`, `storage_location` (incl. `active`), `series`, `file` (partial
unique index on `(project_id, path)` where `deleted_at IS NULL`),
`file_storage_location` (unique `(file_id, storage_location_id)`, state enum).
`alembic check` reports no differences from the models. SQLite runs in WAL
mode, and write transactions use `BEGIN IMMEDIATE`.
*Verify:* unit test on a fresh SQLite file plus `alembic check`.

**DB-03 (M1)** On start, projects and storage locations from config are upserted
into the DB by name. A storage location no longer in config is set
`active = false` and never deleted; it is reactivated if it returns.
*Verify:* unit test that changes config between two starts.

**DB-04 (M1)** `heliopuddle server start` runs migrations by default;
`--no-migrate` skips them, and if the schema is then not at head, startup fails
with a clear message. `heliopuddle db upgrade` runs the migrations only,
starts no API, UI or services, and exits 0 on success and non-zero on
failure.
*Verify:* unit tests via the CLI runner: fresh DB, already at head, a failing
migration, and `--no-migrate` against an old schema.

**DB-05 (M3)** The schema has `index_progress` (`storage_location_id`
primary key, `last_full_at`, `last_incremental_at`); the schema matches the
models on SQLite and Postgres.
*Verify:* unit test (SQLite); integration test (Postgres).

**DB-06 (M4)** On SQLite and Postgres the schema has `rule_state(project_id, rule_name, enabled, watermark_at,
watermark_id, last_success_at)`, `file.ignored_from_all_rules` (default
false), index-progress chain/cursor columns and an index supporting
`(updated_at, id)`. Schema and ORM agree.
*Verify:* schema comparison on both databases and round-trip
cursor/enablement tests.

**DB-01b (M2)** The same migrations run on Postgres, and
the schema matches the models (the `alembic check` equivalent is clean) on
SQLite and Postgres.
*Verify:* integration test with a Postgres testcontainer.

**DB-02 (M2)** Concurrent registrations and removals in one series (threads,
each with its own session) leave exactly one live `is_latest` file,
`series.latest_file_id` pointing at it, and unique live paths. On SQLite only
write transactions use `BEGIN IMMEDIATE` (reads are deferred); on Postgres the
series row is locked with `SELECT ... FOR UPDATE`.
*Verify:* concurrency tests: unit on SQLite, integration on Postgres.

**DB-07 (M6A)** On SQLite and Postgres `file` has
`version_number BIGINT NULL` and `version_text VARCHAR(255) NULL`
(`COLLATE "C"` on Postgres) with a `CHECK` that at most one is set and the
indexes `(series_id, version_number DESC)` and `(series_id, version_text
DESC)`; `series.version_type` is nullable. Schema and ORM agree.
*Verify:* schema comparison and the `CHECK` on both databases; byte-wise ordering of `version_text` on Postgres.

**DB-08 (M6A)** `file_storage_location.arrived_at` (UTC, not null) exists on
SQLite and Postgres. Schema and ORM agree.
*Verify:* schema comparison and a round trip on both databases.

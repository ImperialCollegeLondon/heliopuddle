# Configuration

Each project is one YAML file. The server reads every `*.yaml` and `*.yml` file
in the directory named by `HELIOPUDDLE_CONFIG_DIR` (default `config`, `/config`
in the Docker image), in file name order. Project names must be unique. With no
project files it writes and loads a `default` project instead (see the
[quick start](quick-start.md)).

- Configuration is validated at startup. A mistake stops the server with the
  file, the field (and the rule name for a rule) and the problem.
- Configuration is read-only through the API: edit the file and restart.
- `${VAR}` and `${VAR:-default}` are expanded from the environment in each
  storage location's `url` and in every string of its `storage_options`. Use it
  for secrets and machine-specific paths. An unset variable with no default is
  an error.
- **Durations** are `<number><unit>` parts: `s`, `m` (minutes), `h`, `d`, `w`,
  `mo` (30 days) and `y` (365 days), such as `30d`, `1h30m` or `1y6mo`.
- **Sizes** are bytes or a string with decimal (`500MB`, `2GB`) or binary
  (`1GiB`, `2TiB`) units.
- **Schedules** are five-field cron expressions, such as `"17 2 * * *"`
  (UTC). Quote them.
- **Globs** are relative paths using `*`, `**` and `?`, matched against the
  whole path inside the storage location, such as `science/**/*.csv`.

A complete file that sets every option is in
[`examples/config-reference/project.yaml`](https://github.com/alastairtree/heliopuddle/blob/main/examples/config-reference/project.yaml),
and is shown in full [at the end](#full-example). A test loads it, so it stays
valid.

## Project

| Option | Type | Default | Meaning |
|---|---|---|---|
| `project` | name | required | Project name: letters, digits, `_`, `.`, `-`. |
| `storage_locations` | list | required | One or more [storage locations](#storage-locations). |
| `index` | mapping | all defaults | [Indexing](#index) settings. |
| `versioning` | list | one default number rule | [Version rules](#versions-versioning). `[]` turns versioning off. |
| `rules` | list | none | [Archiving rules](#rules). |
| `reconcile` | mapping | all defaults | [Reconcile](#reconcile) settings. |

## Storage locations

Each entry of `storage_locations`. Any
[fsspec](https://filesystem-spec.readthedocs.io/) backend works: local, SMB,
SFTP, S3, Azure Blob and GCS (install the matching extra).

| Option | Type | Default | Meaning |
|---|---|---|---|
| `name` | name | required | Used by rules, the API and the UI. Unique in the project. |
| `url` | string | required | fsspec URL of the root, such as `file:///data` or `s3://bucket/prefix`. |
| `storage_options` | mapping | `{}` | Passed straight to fsspec (credentials and so on). |
| `accept_external_changes` | bool | `true` | Tolerate files changed in the store by something other than hp. |
| `create_root` | `true`, `false` or `auto` | `auto` | Create a missing root. `auto` creates it except on object stores, whose bucket or container must already exist. |
| `quota` | size | none | Cap on the indexed size of the location. Rules check it before copying or moving in. |

## Index

The `index` block. Every key is optional.

| Option | Type | Default | Meaning |
|---|---|---|---|
| `full_schedule` | cron | none | When to run a full index. Without it, run on demand. |
| `incremental_schedule` | cron | none | When to run an incremental index. |
| `batch_size` | int | `1` | Listed entries handled per `index_batch` task. |
| `batch` | [batch limits](#batch-limits) | see below | When one index run stops and continues in a follow-up. |
| `retrigger` | bool | `true` | Start a follow-up run while work remains. |
| `pause_between_runs` | duration | `30s` | Wait before a follow-up run. |
| `settle_time` | duration | `60s` | Skip files modified more recently than this (they may still be written). |
| `reverify_after` | duration | `1h` | Repair copies stuck in `pending` or `deleting` for longer than this. |
| `lock_timeout` | duration | `1h` | How long a full index waits for the index lock. |
| `incremental_overlap` | duration | `5m` | Re-check files modified just before the last watermark. |
| `incremental_lookback` | duration | `2d` | How many days back `incremental_prefixes` expands. |
| `incremental_prefixes` | list of templates | `[]` (whole location) | Only list these folders in incremental runs. Use `{date:<strftime>}`, such as `science/{date:%Y/%m/%d}`. |

### Batch limits

Used by `index.batch` and `options.batch` of a rule. A run stops after the file
that reaches the first limit and continues in a follow-up run. Each limit is
optional and must be positive, but at least one must stay set.

| Option | Type | Default | Meaning |
|---|---|---|---|
| `max_files` | int | `100` | Files per run. |
| `max_bytes` | size | `1GB` | Bytes per run. |
| `max_duration` | duration | `30m` | Time per run. |

## Versions (`versioning`)

The file name decides a file's series and version. The `versioning` list holds
rules tried in order: the first rule whose `paths` match **and** whose
`extraction_pattern` is found in the file name decides. A file matched by no
rule is unversioned. If you set nothing, a project gets one rule that reads
`file_v12.csv` as version 12; set `versioning: []` to switch versioning off.

| Option | Type | Default | Meaning |
|---|---|---|---|
| `paths` | list of globs | required | Which files the rule applies to. |
| `version_type` | `number` or `text` | `number` | `number` is an integer; `text` compares as a string. |
| `extraction_pattern` | regex | required | Searched in the **file name**. A named group `version` is the version, else the whole match. A named group `date` (`YYYYMMDD` or `YYYY-MM-DD`) also sets the content date. |
| `version_text` | format string | none | `text` rules only: builds the version from the named groups, such as `{major:0>3}.{minor:0>4}`. Pad numbers so string order is version order. |
| `ignore_in_series` | list of group names | `[]` | Named groups that do not split a series, such as `ext` to treat `.csv` and `.json` as one series. |

```yaml
--8<-- "examples/config-reference/project.yaml:versioning"
```

## Rules

Rules copy, move or delete files on a schedule or on demand. Each rule selects
files in a `source` storage location and acts on them in batches. Only `name`,
`action`, `source`, `match.paths` and (for copy and move) `destination` are
required.

| Option | Type | Default | Meaning |
|---|---|---|---|
| `name` | name | required | Unique in the project. |
| `action` | `copy`, `move` or `delete` | required | `move` deletes the source only after the destination hash is verified. |
| `source` | storage location | required | Where files are read from. |
| `destination` | storage location | required for copy and move | Where files go. Not allowed for `delete`; must differ from `source`. |
| `match` | mapping | required | Which files to select, below. |
| `options` | mapping | all defaults | How to transfer, below. |
| `enabled` | bool | `true` | `false` stops the rule running. It can also be toggled at runtime, and both must allow it. |
| `schedule` | cron | none | Without it the rule runs on demand and when matching files change. |

### `match`

| Option | Type | Default | Meaning |
|---|---|---|---|
| `paths` | list of globs | required | Include globs. |
| `exclude` | list of globs | `[]` | Exclude globs. |
| `is_latest` | bool or `null` | `null` | `null`: every version. `true`: only the latest. `false`: all but the latest. |
| `age` | mapping | no age filter | Age criteria. Every criterion given must hold. |

`age` takes up to four dates; each takes `older_than`, `younger_than` or both (a
range):

| Date | Meaning |
|---|---|
| `modified` | Modified time of the source copy. |
| `created` | When hp first recorded the file. |
| `content_date` | The file's content date. A file without one never matches. |
| `arrived` | When the file arrived in the source location (including by a rule). Use it as a cooling-off period. |

A bound is a duration counted back from the start of the run (`30d`) or an
absolute ISO date or time (`2026-01-01`, UTC when no zone is given).
`older_than` selects dates before the cutoff and `younger_than` dates at or
after it. A range that can never match is rejected.

### `options`

| Option | Type | Default | Meaning |
|---|---|---|---|
| `overwrite` | bool | `false` | Replace a different file already at the destination. |
| `check_for_space` | bool | `true` | Check the destination has room before transferring. |
| `check_for_space_overhead` | size | `2GB` | Free space to keep beyond the file being transferred. |
| `update_only_if_newer` | bool | `false` | When the destination has different content, skip it if the destination copy is newer than the source (instead of overwriting or reporting a conflict). |
| `dry_run` | bool | `false` | Log what would happen and change nothing. |
| `retrigger` | bool | `true` | Start a follow-up run while work remains. |
| `pause_between_runs` | duration | `30s` | Wait before a follow-up run. |
| `batch` | [batch limits](#batch-limits) | see above | When a run stops and continues. |

```yaml
--8<-- "examples/config-reference/project.yaml:rules"
```

## Reconcile

Settings for reconcile runs, which bring files in line with the rules.

| Option | Type | Default | Meaning |
|---|---|---|---|
| `schedule` | cron | none | When to reconcile. Without it, run on demand. |
| `verify` | bool | `false` | Re-hash available copies on scheduled runs. |
| `rules` | list of rule names | `null` (all rules) | Rules to reconcile on scheduled runs. |
| `max_rule_passes` | int | `3` | Rule passes to reach a stable state. |
| `max_failures` | int | `5` | File failures before a pass stops. |

## Full example

```yaml
--8<-- "examples/config-reference/project.yaml"
```

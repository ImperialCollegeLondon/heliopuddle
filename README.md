# heliopuddle

**heliopuddle** (hp) is a catalogue for a file store/datalake with a web UI, 
CLI and python client library. It is particularly well suited to space science 
datastores containing large numbers of files like mission_[date]_[version].csv 
because it has great support for complex filename based versioning schemes but 
any folder structure of data files becomes easier to manage with heliopuddle. It 
is designed to work great with 1000s of CSV/CDF/Parquet files spread over 
multiple storage locations (local/SMB/S3/Azure Blob/etc). All background work 
runs as [Prefect](https://www.prefect.io/) flow runs and so scales to multiple 
nodes and high availability easily.

> **Status:** alpha. The specification and design are in
> [`docs/spec`](docs/spec) and [`docs/design`](docs/design). Code is being built
> milestone by milestone (see [`docs/spec/features.md`](docs/spec/features.md)).

**Documentation:** <https://ImperialCollegeLondon.github.io/heliopuddle/> (quick start,
Docker, Keycloak, environment variables; source in [`docs/site`](docs/site)).

## Origin

heliopuddle was extracted from the datastore, archiving and file-transfer work
done for the IMAP MAG instrument's data pipeline at Imperial College London but
is a general-purpose tool.

## What it does

- **Projects and storage locations.** A project is a set of files stored in one or more
  storage locations. The first storage location of every project is an
  [fsspec](https://filesystem-spec.readthedocs.io/) filesystem (local disk,
  network share, Azure Blob, S3, ...). SFTP and rclone targets (SharePoint,
  OneDrive, Box, ...) can be added later as sync targets for a subset of files.
- **A DB index of every copy.** Each logical file has one row, with a hash,
  size, version and metadata, plus one row per copy in each storage location with its
  state (`pending`, `available`, `missing`, `deleting`, `deleted`, `unknown`).
- **Versions from paths.** Versioning is configured per path with regexes:
  a version is a number (`report_v12.csv` is 12) or a normalised text
  (`x_v1.4.csv` is `001.0004`). Files in one series are ordered by version,
  and the newest is `is_latest`. A client can also give the version itself
  when it adds a file.
- **Clients write directly to the store.** A client asks the API for the
  storage location config, writes the file itself with fsspec, then registers it.
  The server verifies the hash in the background.
- **External changes are accepted.** Index flows pick up files that are added,
  changed or removed directly in the store, so an existing folder or bucket
  can be adopted as it is.
- **Archiving rules.** Rules `copy`, `move` or `delete` files that match path
  globs, age, content date and `is_latest`. A source file is never deleted
  until the destination's hash has been verified.
- **Simple to run.** One command, `heliopuddle server start`, runs the API, the
  web UI, the background services and (if needed) an embedded Prefect server.
  The same command runs in the Docker image. SQLite by default, Postgres
  optionally.

## Storage backends

Any [fsspec](https://filesystem-spec.readthedocs.io/) backend can be a storage
location. Local disks, SMB shares, SFTP, S3, Azure Blob and Google Cloud
Storage are supported out of the box:

Default install `heliopuddle` is small and contains only the client library
for interacting with a server instance. Add optional pip dependencies

- `cli` for the command line app `heliopuddle file add some_file ...`
- `server` to be able to actually start an instance of the app
  `heliopuddle server start`
- a storage backend: `s3`, `azure`, `gcs`, `sftp` or `smb` (local disks need
  none); `all` is the server, the CLI and every backend.

```bash
pip install heliopuddle                   # the Python client only (httpx, pydantic, fsspec)
pip install "heliopuddle[cli]"            # the client plus the `heliopuddle file` / `user` commands
pip install "heliopuddle[server,azure]"   # a server whose storage locations are on Azure Blob
pip install "heliopuddle[all]"            # server, CLI and every backend
```

The client's requirements are deliberately loose (`pydantic>=2.5`,
`httpx>=0.24`, `fsspec>=2023.1`, `typer>=0.13` for the CLI; tested in CI at
exactly those versions) so it fits next to your own pins.

See [`docs/design/storage.md`](docs/design/storage.md).

## Quick start

```bash
uv sync
uv run heliopuddle server start   # no config needed: API + UI on :8080, services, embedded Prefect
```

On first start hp creates an `admin` user and logs its generated password once
(set `HELIOPUDDLE_ADMIN_USERNAME` / `HELIOPUDDLE_ADMIN_PASSWORD` to choose
them). Sign in to the UI at `/`. To create more users from the command line, say
who you are (the admin account; the CLI asks for its password) and name the new
user:

```bash
export HELIOPUDDLE_USERNAME=admin        # the account you log in as
uv run heliopuddle user add alice --roles hp-reader,hp-writer   # alice is the NEW user
```

Authorization is by roles (`hp-reader`,
`hp-writer`, `hp-deleter`, `hp-operator`, `hp-client-storage-read-write`, `hp-admin`).
All except `hp-admin` can be project-scoped, for example `hp-writer:demo`.
Use an external Keycloak with
`HELIOPUDDLE_AUTH_MODE=oidc` ([examples/keycloak](examples/keycloak)), or
`HELIOPUDDLE_AUTH_MODE=header` behind a proxy that authenticates users. See
[`docs/design/auth.md`](docs/design/auth.md).

Readers and writers transfer file content through the API. For direct fsspec
access, also grant `hp-client-storage-read-write` (optionally project-scoped,
for example `hp-client-storage-read-write:demo`); only that role or `hp-admin`
can retrieve backing-storage credentials. Restrict issued backend credentials
to the project's root using backend permissions.

With no project files, hp writes `./.heliopuddle/default_config.yaml` (a project
`default` with storage locations `primary` and `archive` in `./.heliopuddle`, created
automatically, version rules and an hourly archive rule)
and loads it; edit that file to change the setup. To configure your own, put
project YAML files in a directory (see [examples/projects](examples/projects)
and the samples below) and set `HELIOPUDDLE_CONFIG_DIR`. 

The database should be initialised automatically but you can do it manually with
`heliopuddle db upgrade`. This runs the migrations only, then exits.

```python
from heliopuddle.client import HelioPuddleClient

hp = HelioPuddleClient(
    "http://localhost:8080/api/v1", project="default", username="admin", password="..."
)
record = hp.add_file("local/report_v001.csv", path="reports/2026/report_v001.csv")
print(record.id, record.state)  # verified on register: "available"

hp.add_file("local/report_v002.csv", path="reports/2026/report_v002.csv")
hp.latest("reports/2026/report_v001.csv").path  # "reports/2026/report_v002.csv"
[f.path for f in hp.list_versions("reports/2026/report_v001.csv").items]
hp.find(path="reports/**/*.csv", latest_only=True)
hp.download("reports/2026/report_v002.csv", "copy.csv")  # checks the md5
hp.remove_file("reports/2026/report_v002.csv")  # v001 becomes latest again
```

Files put straight into a storage location are picked up by the index flows
(scheduled from the project's `index:` config, or `hp.trigger_index()`), and an
existing folder or bucket is adopted on first start.

Re-adding identical content is a no-op; different content at an existing path
raises `ConflictError` unless `overwrite=True`.

From the command line (`pip install "heliopuddle[cli]"`):

```bash
export HELIOPUDDLE_API_URL=http://localhost:8080/api/v1 HELIOPUDDLE_PROJECT=default
export HELIOPUDDLE_USERNAME=admin                 # password from a prompt or HELIOPUDDLE_PASSWORD
heliopuddle file add local/report_v001.csv reports/2026/report_v001.csv
heliopuddle file add ./outbox reports/batch --recursive
heliopuddle file add local/a.csv reports/a.csv --metadata '{"source": "lab"}' --content-date 2026-03-04

# A file another tool already wrote to the store: record it (nothing is uploaded)
heliopuddle file register reports/2026/report_v002.csv --hash "$(md5sum report_v002.csv | cut -d' ' -f1)" --size 1234

heliopuddle file list "reports/**/*.csv" --latest
heliopuddle file show 42                                  # by id; a number is always an id
heliopuddle file show --path reports/2026/report_v001.csv # or by path (a glob) ...
heliopuddle file show --name report_v001.csv              # ... or by file name in any folder
heliopuddle file download --path reports/2026/report_v001.csv --to .
heliopuddle file remove --path "reports/tmp/**"           # lists the files and asks; --yes to skip
```

Change metadata and the content date of files already added (by id,
`--path` or `--name`, one or many):

```bash
heliopuddle file update 42 --set quality=good --set run=7   # VALUE is JSON if it parses, else a string
heliopuddle file update 42 --unset quality                  # remove a key
heliopuddle file update --path "reports/2026/*" --metadata '{"reviewed": true}'
heliopuddle file update 42 --metadata '{"only": "this"}' --replace-metadata
heliopuddle file update 42 --content-date 2026-03-04T12:00:00
heliopuddle file update 42 --clear-content-date             # back to the date in the path, or none
```

A file's version comes from its path through the project's version rules
(e.g. `report_v002.csv` is version 2), so add a new version under its new
path. To give the version yourself, pass it when the file is created; it can
never be changed afterwards:

```bash
heliopuddle file add notes.txt reports/notes.txt --version-number 7
heliopuddle file add cal.json cal/cal.json --version-text 2026.03
```

```python
hp.add_file("local/notes.txt", path="reports/notes.txt", version_number=7)
```

An application that writes the store itself registers its files instead of
uploading them (nothing is read or written by the client):

```python
record = hp.register_file("reports/2026/report_v003.csv", hash=md5_hex, size=n_bytes)
hp.update_metadata(record.id, {"reviewed": True})  # None removes a key
hp.set_content_date("reports/2026/report_v003.csv", datetime(2026, 3, 4, tzinfo=UTC))
```

### Testing code that calls heliopuddle

`heliopuddle.testing.FakeHelioPuddleClient` is an in-memory drop-in for the
client (no server, no Docker, client install only), and installing heliopuddle
gives pytest the `heliopuddle_fake` fixture:

```python
def test_publish_registers_the_report(heliopuddle_fake, heliopuddle_store):
    (heliopuddle_store / "reports").mkdir()
    (heliopuddle_store / "reports/a.csv").write_text("x")
    my_app.publish("reports/a.csv", client=heliopuddle_fake)

    assert heliopuddle_fake.live_paths() == ["reports/a.csv"]
    assert heliopuddle_fake.methods_called() == ["register_file"]


def test_publish_retries_a_busy_path(heliopuddle_fake):
    heliopuddle_fake.inject_busy("reports/a.csv", times=2)
    heliopuddle_fake.inject_error("remove_file", ConnectionError("down"))
    ...
```

A contract test in this repository runs the same scenarios against the fake
and the real API, so the fake stays faithful.

Or with Docker, using the published image (`:latest` is the newest release;
`:main` follows the main branch and `:pr-<number>` is a pull request's build):

```bash
docker run -p 8080:8080 \
  -v $PWD/data:/data -v $PWD/examples/projects:/config \
  -v hp-state:/var/lib/heliopuddle \
  ghcr.io/imperialcollegelondon/heliopuddle:latest
```

The image also runs an embedded Prefect server on port 4200. Its API has **no
authentication**, so do not publish it publicly: add `-p 127.0.0.1:4200:4200`
to reach it from the same machine only, and use an authenticating reverse proxy
for remote access (see [deployment](docs/design/deployment.md)).

## Sample configuration

A project is one YAML file in the config directory. Configuration is
read-only through the API; change it by editing the file and restarting.

### Minimal project

```yaml
project: demo
storage_locations:
  - name: primary
    url: file:///data
    accept_external_changes: true
```

### Version rules

Without a `versioning` section a project gets one rule that reads number
versions such as `file_v8.csv`, `file_V09.csv` and `file_v10.csv` (8, 9, 10):

```yaml
versioning:
  - paths: ["**"]
    version_type: number    # the default
    # the named group 'version' holds the number
    extraction_pattern: '_[vV](?P<version>\d{1,5})\.[^./]+$'
```

Without a `version` group, the whole match is the version:

```yaml
versioning:
  - paths: ["**"]
    extraction_pattern: '[0-9]+(?=\.[^./]+$)'   # any digits before the extension
```

Text versions handle more complex naming. Named groups are put into a Python
format string, which can pad them so that versions sort correctly as text:

```yaml
versioning:
  # major.minor versions (up to 3-digit major, 4-digit minor) in names like
  # file_v001.1234.csv, file_v2.12.csv, file_v0.0001.csv
  - paths: ["**"]
    version_type: text
    extraction_pattern: '_v(?P<major>\d{1,3})\.(?P<minor>\d{1,4})\.(?P<ext>[^./]+)$'
    version_text: '{major:0>3}.{minor:0>4}'   # file_v2.12.csv -> "002.0012"
    # ignore_in_series: [ext]                 # uncomment: .csv and .json versions share a series
```

Files whose names differ only in the version are one series, and the highest
version is latest. A different extension is a different series unless the
group is listed in `ignore_in_series`. See
[docs/design/versioning.md](docs/design/versioning.md).

### Two storage locations with archiving

```yaml
project: demo
storage_locations:
  - name: primary
    url: file:///data
    accept_external_changes: true
  - name: archive
    url: az://archive/demo
    storage_options:
      connection_string: "${AZURE_CONN}"   # resolved from the environment
    accept_external_changes: false

versioning:                                 # first matching rule wins
  - paths: ["calibration/layers/**"]
    version_type: text
    extraction_pattern: '_v(?P<major>\d{1,3})\.(?P<minor>\d{1,4})\.(?P<ext>[^./]+)$'
    version_text: '{major:0>3}.{minor:0>4}'
  - paths: ["**"]
    extraction_pattern: '_[vV](?P<version>\d{1,5})\.[^./]+$'

index:
  incremental_schedule: "*/10 * * * *"
  full_schedule: "0 3 * * *"

rules:
  - name: archive-superseded-science
    action: move
    source: primary
    destination: archive
    match:
      paths: ["science/**/*.cdf"]
      is_latest: false
      age:                        # all criteria must hold
        modified: {older_than: 30d}
        arrived: {older_than: 1d}   # cooling-off after arriving in primary (or a move back)
    options:
      overwrite: false
      check_for_space: true
      batch: { max_files: 500 }
    schedule: "17 2 * * *"
```

### Sample: a space-instrument science datastore

This sample shows how a mission science datastore with housekeeping, science
levels, quicklooks and space-weather feeds could be managed. It keeps only the
latest version of each product online and deletes stale feeds.

```yaml
project: instrument-datastore
storage_locations:
  - name: datastore
    url: file:///data/datastore
    accept_external_changes: true

versioning:
  - paths: ["spice/**"]
    extraction_pattern: '_(?P<version>\d{2})\.[^./]+$'
  # science and HK products: _v000.ext
  - paths: ["**"]
    extraction_pattern: '_[vV](?P<version>\d{1,5})\.[^./]+$'

rules:
  - name: delete-non-latest-hk-l1
    action: delete
    source: datastore
    match:
      paths: ["**/hk/mag/l1/**", "**/hk/sc/l1/**"]
      is_latest: false
      age: {modified: {older_than: 24h}}
    schedule: "0 * * * *"

  - name: delete-non-latest-science
    action: delete
    source: datastore
    match:
      paths: ["**/science/mag/l1b/**", "**/science/mag/l1c/**", "**/science/mag/l1d/**", "**/science/mag/l2/**"]
      is_latest: false
      age: {modified: {older_than: 1h}}
    schedule: "5 * * * *"

  - name: delete-preliminary-l2-after-6-months
    action: delete
    source: datastore
    match:
      paths: ["**/science/mag/l2-pre/*/*/*_l2-pre_*.cdf"]
      age: {modified: {older_than: 180d}}
    schedule: "20 3 * * *"

  - name: delete-unused-frames
    action: delete
    source: datastore
    match:
      paths:
        - "**/science/mag/l1d/*/*/*-dsrf_*.cdf"
        - "**/science/mag/l1d/*/*/*-srf_*.cdf"
        - "**/science/mag/l1d/*/*/*-gsm_*.cdf"
        - "**/science/mag/l2/*/*/*-dsrf_*.cdf"
        - "**/science/mag/l2/*/*/*-srf_*.cdf"
        - "**/science/mag/l2/*/*/*-gsm_*.cdf"
      age: {modified: {older_than: 24h}}
    schedule: "25 3 * * *"

  - name: delete-old-quicklooks
    action: delete
    source: datastore
    match:
      paths: ["**/quicklook/**/*.png"]
      age: {modified: {older_than: 90d}}
    schedule: "30 3 * * *"

  - name: delete-space-weather-feeds-after-7-days
    action: delete
    source: datastore
    match:
      paths: ["**/noaa/**/*.csv"]
      age: {modified: {older_than: 7d}}
    schedule: "35 3 * * *"
```

## Documentation

- [`docs/spec/features.md`](docs/spec/features.md): milestones and feature scope.
- [`docs/spec/requirements/`](docs/spec/requirements): requirement IDs per
  component, used by the tests.
- [`docs/design/`](docs/design): architecture, data model, versioning,
  operations, indexing, rules and deployment.

## Development

```bash
uv sync
uv run pytest tests/unit
uv run pytest tests/integration      # needs Docker (Postgres, Azurite)
uv run ruff check . && uv run pyright
uv run lint-imports                  # import contracts (client/UI never touch the DB)
uv run pre-commit install            # ruff, pyright and lint-imports on commit
```

### Running the e2e tests

The e2e suite builds the Docker image and drives it from the host with the
client and Playwright. It is skipped when Docker is not available.

```bash
uv run playwright install chromium   # once (or set HP_CHROMIUM=/path/to/chrome)
uv run pytest tests/e2e --req-report=.reports/req-e2e.json
HP_E2E_IMAGE=heliopuddle:dev uv run pytest tests/e2e   # reuse an image you built
```

See [`CLAUDE.md`](CLAUDE.md) for conventions.

## Licence

See [LICENSE](LICENSE).

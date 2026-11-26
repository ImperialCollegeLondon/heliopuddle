# Quick start

The default install is small: only the Python client. Add extras for the rest.

| Install | What you get |
|---|---|
| `pip install heliopuddle` | the Python client |
| `pip install "heliopuddle[cli]"` | the client and the `heliopuddle file` / `user` commands |
| `pip install "heliopuddle[server]"` | a server (`heliopuddle server start`) |
| `pip install "heliopuddle[server,azure]"` | a server with Azure Blob storage (also `s3`, `gcs`, `sftp`, `smb`) |
| `pip install "heliopuddle[all]"` | server, CLI and every storage backend |

The `heliopuddle` command is installed with every option, but without the `cli`
extra it only prints how to install it.

With [uv](https://docs.astral.sh/uv/) you can install it as a tool instead:
`uv tool install "heliopuddle[server]"`.

## Or run it with Docker

The server is also published as a Docker image,
[`heliopuddle/heliopuddle`](https://hub.docker.com/r/heliopuddle/heliopuddle),
with the server, CLI and every storage backend installed:

```bash
docker run -p 8080:8080 -p 127.0.0.1:4200:4200 \
  -v hp-state:/var/lib/heliopuddle heliopuddle/heliopuddle
```

With no settings it is a complete server: the API and UI on
<http://localhost:8080>, SQLite and an embedded Prefect server on port 4200
(published on loopback only, as it has no authentication), and an example
project. The `admin`
password is in the container log (`docker logs <container>`). See
[Docker](docker.md) for volumes, settings and a Compose setup with Postgres.

## Run the server

```bash
heliopuddle server start
```

With no configuration this runs the API and UI on <http://127.0.0.1:8080>, the
background services and an embedded Prefect server, with SQLite under
`./.heliopuddle`. It creates a project called `default` (written to
`./.heliopuddle/default_config.yaml`) with storage locations `primary` and
`archive`; edit that file to change the setup.

On first start an `admin` user is created and its generated password is logged
once. Set `HELIOPUDDLE_ADMIN_USERNAME` and `HELIOPUDDLE_ADMIN_PASSWORD` (at least
12 characters) to choose them, then sign in to the UI at `/`.

To use your own projects, put one YAML file per project in a directory and set
`HELIOPUDDLE_CONFIG_DIR`. A minimal project with one local storage location:

```yaml
project: demo
storage_locations:
  - name: primary
    url: file://${HELIOPUDDLE_DEMO_DATA:-/data}
    accept_external_changes: true
```

`${VAR}` and `${VAR:-default}` are expanded from the environment. Configuration
is read-only through the API: edit the file and restart. See
[Configuration](configuration.md) for every option, including versions and rules.

Create more users (the CLI logs in as the account named by `HELIOPUDDLE_USERNAME`
and asks for its password):

```bash
export HELIOPUDDLE_USERNAME=admin
heliopuddle user add alice --roles hp-reader,hp-writer
```

Roles are `hp-reader`, `hp-writer`, `hp-deleter`, `hp-operator`,
`hp-client-storage-read-write` and `hp-admin`; all but `hp-admin` can be scoped
to a project, for example `hp-writer:demo`.

## Use the command line

```bash
export HELIOPUDDLE_API_URL=http://localhost:8080/api/v1 HELIOPUDDLE_PROJECT=default
export HELIOPUDDLE_USERNAME=admin     # password from a prompt or HELIOPUDDLE_PASSWORD

heliopuddle file add local/report_v001.csv reports/2026/report_v001.csv
heliopuddle file add ./outbox reports/batch --recursive
heliopuddle file list "reports/**/*.csv" --latest
heliopuddle file show --path reports/2026/report_v001.csv
heliopuddle file download --path reports/2026/report_v001.csv --to .
heliopuddle file update 42 --set quality=good
heliopuddle file remove --path "reports/tmp/**"
```

`heliopuddle file` also has `register` (record a file another tool already wrote
to the store). Run `heliopuddle file --help` or `heliopuddle file add --help`
for every option.

## Use the Python client

```python
from heliopuddle.client import HelioPuddleClient

hp = HelioPuddleClient(
    "http://localhost:8080/api/v1", project="default", username="admin", password="..."
)
record = hp.add_file("local/report_v001.csv", path="reports/2026/report_v001.csv")
print(record.id, record.state)  # "available" once the hash is verified

hp.latest("reports/2026/report_v001.csv").path
hp.find(path="reports/**/*.csv", latest_only=True)
hp.download("reports/2026/report_v001.csv", "copy.csv")  # checks the md5
```

Re-adding identical content is a no-op; different content at an existing path
raises `ConflictError` unless `overwrite=True`.

Keep a path in step with a local file, e.g. from an application that mirrors
its own files into heliopuddle. `sync_file` registers or uploads a new file,
replaces changed content, updates the metadata and content date, and checks
the result; `sync_removed` removes a live file. Both return the action taken
and accept `dry_run=True`. Pass `in_place=True` when the content is already at
the path in the storage location (a store you share with heliopuddle), so
nothing is uploaded:

```python
hp = HelioPuddleClient(
    api_url,
    project="default",
    client_id="...",
    client_secret="...",
    busy_retries=3,  # retry a path another operation owns
)
result = hp.sync_file(
    "/data/reports/r_v001.csv", "reports/r_v001.csv", metadata={"source": "lab"}, in_place=True
)
print(result.action)  # added, updated, metadata or unchanged
hp.sync_removed("reports/old.csv").action  # removed, or unchanged if nothing was live
md5_hex, size = file_digest("/data/reports/r_v001.csv")  # from heliopuddle.client
```

### Testing code that uses heliopuddle

Installing heliopuddle gives pytest the `heliopuddle_fake` and
`heliopuddle_store` fixtures: an in-memory stand-in for the client, with no
server or Docker needed.

```python
def test_publish_registers_the_report(heliopuddle_fake, heliopuddle_store):
    (heliopuddle_store / "reports").mkdir()
    (heliopuddle_store / "reports/a.csv").write_text("x")
    my_app.publish("reports/a.csv", client=heliopuddle_fake)

    assert heliopuddle_fake.live_paths() == ["reports/a.csv"]
    assert heliopuddle_fake.methods_called() == ["register_file"]
```

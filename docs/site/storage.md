# Storage locations

A project stores its files in one or more storage locations. Each is an
[fsspec](https://filesystem-spec.readthedocs.io/) URL plus `storage_options`,
the keyword arguments for that backend:

```yaml
storage_locations:
  - name: primary
    url: file:///data/project
  - name: archive
    url: s3://my-bucket/project
    storage_options: { key: "${AWS_ACCESS_KEY_ID}", secret: "${AWS_SECRET_ACCESS_KEY}" }
```

The first storage location receives uploads. Others are filled by rules, by
indexing files that are already there, or both.

## Backends

| Backend | URL | Install |
|---|---|---|
| Local disk or mounted share | `file:///data/project` | included |
| SMB / Windows share | `smb://server/share/project` | `heliopuddle[smb]` |
| SFTP | `sftp://host/srv/project` | `heliopuddle[sftp]` |
| S3 and S3-compatible | `s3://bucket/project` | `heliopuddle[s3]` |
| Azure Blob / ADLS | `az://container/project` | `heliopuddle[azure]` |
| Google Cloud Storage | `gs://bucket/project` | `heliopuddle[gcs]` |
| rclone remote (SharePoint, OneDrive, Box, ...) | `rclone://remote/project` | the `rclone` program, see [rclone](#rclone-sharepoint-onedrive-box) |

`heliopuddle[all]` installs every backend, as the Docker image does. Any other
fsspec backend works once its package is installed.

### Examples

```yaml
storage_locations:
  - name: share                     # SMB
    url: smb://fileserver/data/project
    storage_options: { username: "${SMB_USER}", password: "${SMB_PASSWORD}" }

  - name: remote                    # SFTP with a key
    url: sftp://data.example.org/srv/project
    storage_options: { username: archive, key_filename: /config/id_ed25519 }

  - name: remote-password           # SFTP with a password and a port
    url: sftp://data.example.org/srv/project
    storage_options: { username: archive, password: "${SFTP_PASSWORD}", port: 2222 }

  - name: s3                        # S3; credentials from the environment (below)
    url: s3://my-bucket/project

  - name: minio                     # S3-compatible
    url: s3://my-bucket/project
    storage_options:
      key: "${S3_KEY}"
      secret: "${S3_SECRET}"
      client_kwargs: { endpoint_url: https://s3.example.org }

  - name: azure
    url: az://archive/project
    storage_options: { connection_string: "${AZURE_STORAGE_CONNECTION_STRING}" }

  - name: gcs
    url: gs://my-bucket/project
    storage_options: { token: /config/gcs-service-account.json }
```

## Credentials

Credentials can come from four places, and they can be mixed.

### 1. In the file

Literal values in `storage_options`. Fine for settings that are not secret;
avoid it for passwords and keys, because the YAML is plain text.

### 2. From the environment

`${VAR}` and `${VAR:-default}` are replaced from the environment in `url` and
in every string of `storage_options` when the server starts. An unset variable
with no default stops the server with a message naming the file and field.

Most backends also read their own variables and files when an option is not
given, so `storage_options` can be left out:

| Backend | Reads |
|---|---|
| S3 (s3fs) | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `AWS_PROFILE`, `~/.aws/` |
| Azure (adlfs) | `AZURE_STORAGE_CONNECTION_STRING`, `AZURE_STORAGE_ACCOUNT_NAME` with `AZURE_STORAGE_ACCOUNT_KEY` or `AZURE_STORAGE_SAS_TOKEN` |
| GCS (gcsfs) | `GOOGLE_APPLICATION_CREDENTIALS`, then the default Google credentials |
| SFTP (paramiko) | keys in `~/.ssh/` and a running ssh-agent |

### 3. From a Prefect Secret block

Write `{{ prefect.blocks.secret.<name> }}`, Prefect's own placeholder syntax,
in `url` or any string of `storage_options`:

```yaml
  - name: share
    url: smb://fileserver/data/project
    storage_options:
      username: archive
      password: "{{ prefect.blocks.secret.smb-password }}"
```

A value that is only a placeholder takes the secret's JSON value when it holds
JSON, so one Secret block can supply a whole mapping:

```yaml
  - name: azure
    url: az://archive/project
    storage_options: "{{ prefect.blocks.secret.azure-archive }}"   # {"connection_string": "..."}
```

Create Secret blocks in the Prefect UI (Blocks, Secret) or in Python:

```python
from prefect.blocks.system import Secret

Secret(value="hunter2").save("smb-password")
Secret(value={"connection_string": "..."}).save("azure-archive")
```

### 4. From a Prefect RemoteFileSystem block

`block: <name>`, used instead of `url`, takes the whole storage location from
a Prefect `RemoteFileSystem` block: its base path is the URL and its settings
are the `storage_options`. Options written in the file are merged over the
block's settings:

```yaml
  - name: archive
    block: archive-s3
    storage_options: { key: "{{ prefect.blocks.secret.archive-key }}" }
```

Prefect stores a `RemoteFileSystem` block's settings as plain text, so keep
secrets in Secret blocks and refer to them with placeholders.

### When a block is missing

The server starts even if a block does not exist yet, since you need it
running to create the block. Until the block exists, the storage location is
**incomplete**:

- its storage page and the storage list show a warning saying what is
  missing, with a link to Prefect's blocks page;
- runs and requests that need it fail with the same message;
- other storage locations work as normal.

Blocks are read when a storage location is used and cached for a minute, so a
block you create or change is picked up within a minute, without a restart.

Values taken from blocks and the environment are hidden in logs and error
messages. The API returns a storage location's URL and options only to users
with the `hp-client-storage-read-write` role.

## A storage location that holds one folder

A storage location can hold just one folder of the project instead of all of
it. Set `path_prefix` to that folder: the storage location's root then holds
what the project calls that folder.

Say `share` holds the whole project, including a folder `upload_mirror/`, and
a SharePoint library should receive only that folder, with its contents at the
top of the library:

```yaml
storage_locations:
  - name: share
    url: file:///data/project
  - name: sharepoint
    url: rclone://sharepoint/Shared Documents/upload_mirror
    path_prefix: upload_mirror
    storage_options: { config_block: sharepoint-rclone }

rules:
  - name: mirror-uploads
    action: copy
    source: share
    destination: sharepoint
    match:
      paths: ["upload_mirror/**"]
    schedule: "*/15 * * * *"
```

- `upload_mirror/2026/report.csv` is copied to
  `Shared Documents/upload_mirror/2026/report.csv` in SharePoint (the
  library's `upload_mirror` folder is the storage location's root).
- The file keeps one name everywhere in heliopuddle: its project path,
  `upload_mirror/2026/report.csv`. Files found in SharePoint by an index are
  added under `upload_mirror/` too.
- Files outside `upload_mirror/` never go to `sharepoint`: rules skip them
  (even a rule matching `**`), and uploading one there is refused.
- `path_prefix` is a plain folder such as `upload_mirror` or `data/mirror`
  (no `..`, wildcards or leading `/`). Changing it later counts as moving the
  storage location: its copies are checked again, and rules into it start
  again from a full run.
- Clients transfer files in such a storage location through the API rather
  than directly.

It works the same for every kind of storage location, not only rclone.

## rclone (SharePoint, OneDrive, Box)

[rclone](https://rclone.org/) reaches more than seventy storage services. An
`rclone://` storage location names a remote from an rclone config and a path
on it:

```yaml
  - name: sharepoint
    url: rclone://sharepoint/Shared Documents/project
    storage_options: { config_block: sharepoint-rclone }
```

### The rclone config

Make the remote with rclone on any machine with a browser:

```bash
rclone config create sharepoint onedrive
```

then copy the remote's section of `rclone.conf` into a Prefect block of type
**RClone Remote Config File [ManagedFileTransfer]**, with fields `remote_name`
(`sharepoint`) and `config_file_contents` (the section). This is the block
prefect-managedfiletransfer uses, so existing blocks work as they are.
heliopuddle registers the block type with Prefect when the server starts, so
it is in the Prefect UI's block catalogue even on a fresh Prefect server.

On a single machine you can use a file instead: `config_file: /config/rclone.conf`.

When rclone refreshes an OAuth token, heliopuddle writes the new config back
to the block (or file) after every session, including failed ones.

!!! warning "The block holds your tokens in plain text"
    `config_file_contents` is an ordinary text field. Anyone who can read
    blocks in the Prefect UI can read the tokens; protect the Prefect UI.

### SharePoint and OneDrive

Use the `onedrive` backend. For a SharePoint document library, choose the
library when `rclone config` asks (`drive_type = documentLibrary`).

SharePoint changes some files after upload, mostly Office documents, so their
size and hash no longer match what was sent. heliopuddle notices this, does
not count the copy, and keeps the source. Data files are not affected.

### Box

For a server, prefer a Box JWT app (`box_config_file` and
`box_sub_type = enterprise`): Box's JWT authentication gets each token by
signing a new assertion and its documentation mentions no refresh token,
whereas a Box OAuth refresh token "can be used ... only once" and expires
after 60 days. File names on Box are case-insensitive and at most 255
characters.

### How heliopuddle runs rclone

heliopuddle runs `rclone rcd` itself, on the machine doing the work, only for
as long as it is needed:

- **One at a time per config.** A Prefect global concurrency limit,
  `rclone:<config block>`, makes sure that only one rclone uses a given config
  at a time across every heliopuddle process and machine, so two processes
  never refresh the same token. Storage locations with different config
  blocks, normally different projects, run in parallel. Work on one config is
  therefore serialised: plan rule schedules and batch sizes with that in mind.
- **Private.** rclone listens on a Unix socket in a private temporary
  directory with a new random password each time, and is stopped (and its
  temporary config deleted) `HELIOPUDDLE_RCLONE_IDLE` (default `2s`) after the
  work is done, or when heliopuddle exits.
- **Requests.** A web request that needs an rclone storage location waits up
  to `HELIOPUDDLE_RCLONE_API_WAIT` for the config to be free, then answers
  `503` with `Retry-After`.

Restrictions:

- An rclone storage location cannot be a project's first storage location.
- Clients transfer files in rclone storage locations through the API, never
  directly.

### Checking copies

Box and SharePoint do not report an md5. Each rclone storage location sets
how a copy written there is checked, with `storage_options.verify`:

| `verify` | Check |
|---|---|
| `auto` (default) | `native` when the service reports a hash, otherwise `content` |
| `native` | the service's own hash of the stored file (Box SHA1, SharePoint and OneDrive QuickXorHash) against the same hash of what was sent; nothing is downloaded |
| `content` | download the copy and compare md5 |
| `size` | compare sizes only |

### The rclone program

The Docker image includes rclone. Elsewhere heliopuddle uses
`HELIOPUDDLE_RCLONE_BINARY` if set, then `rclone` on the `PATH`, and otherwise
downloads a pinned rclone release (its SHA256 checked) into the data
directory. Set `HELIOPUDDLE_RCLONE_AUTO_DOWNLOAD=false` to turn the download
off; a storage location then stays incomplete until rclone is installed.

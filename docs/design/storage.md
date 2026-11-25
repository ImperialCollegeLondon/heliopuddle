# Storage

hp stores files through [fsspec](https://filesystem-spec.readthedocs.io/).
**Any fsspec backend can be a storage location.** hp's own code uses only the
generic fsspec filesystem API — `open`, `info`, `find`, `exists`, `rm`, `mv`,
`makedirs`, `copy` — so the same code path serves a local disk, a network share, an
SFTP server or a cloud bucket.

## Supported backends

Out of the box (install `heliopuddle[all]`, as the Docker image does):

| Backend | URL form | Package (extra) |
|---|---|---|
| Local disk / mounted share | `file:///data` | fsspec (core) |
| SMB / Windows share | `smb://server/share/prefix` | smbprotocol (`smb`) |
| SFTP | `sftp://host/path` | paramiko, fsspec's SFTP dependency (`sftp`) |
| S3 and S3-compatible | `s3://bucket/prefix` | s3fs (`s3`) |
| Azure Blob / ADLS | `az://container/prefix`, `abfs://...` | adlfs (`azure`) |
| Google Cloud Storage | `gs://bucket/prefix` | gcsfs (`gcs`) |
| rclone remote (SharePoint, OneDrive, Box, ...) | `rclone://remote/path` | the rclone binary, see [rclone](#rclone) |

Any other fsspec implementation works once its package is installed; nothing
in hp needs to change.

Credentials and connection settings go in the storage location's
`storage_options`, passed straight to fsspec; see [Credentials](#credentials).

```yaml
storage_locations:
  - name: primary
    url: s3://my-bucket/project
    storage_options: { key: "${AWS_ACCESS_KEY_ID}", secret: "${AWS_SECRET_ACCESS_KEY}" }
  - name: share
    url: smb://fileserver/data/project
    storage_options: { username: "${SMB_USER}", password: "{{ prefect.blocks.secret.smb-password }}" }
  - name: remote
    url: sftp://data.example.org/srv/project
    storage_options: { username: "${SFTP_USER}", key_filename: /config/id_ed25519 }
  - name: azure
    block: azure-archive          # a Prefect RemoteFileSystem block
  - name: gcs
    url: gs://my-bucket/project
    storage_options: { token: /config/gcs-service-account.json }
  - name: sharepoint
    url: rclone://sharepoint/Shared Documents/project
    storage_options: { config_block: sharepoint-rclone }
```

Administrators receive the same URL and storage options from the API (with
Prefect block placeholders resolved) and write to the store directly, except
for rclone storage locations (`direct_access: false`), which clients always
reach through the API, so a client needs the same backend package installed
(`pip install heliopuddle[s3]`, ...).
In `local` and `oidc` auth modes these details require
`hp-client-storage-read-write` on the project (or `hp-admin`); storage-location
lists withhold the URL without that role. Ordinary readers/writers transfer
content through the API without receiving server credentials.
Trusted-proxy `header` mode retains direct access. See the
[auth security restriction](auth.md#backend-credentials-and-direct-file-access).

## Credentials

A storage location's credentials can come from four places; they can be mixed.

1. **Inline.** Literal values in `storage_options`. Fine for non-secret
   settings; avoid for secrets (the YAML is plain text).
2. **Environment.** `${VAR}` and `${VAR:-default}` in `url` and every string
   of `storage_options`, resolved when the config is loaded; an unset variable
   with no default stops startup. Backends also read their own variables and
   files when an option is not given: s3fs `AWS_ACCESS_KEY_ID`,
   `AWS_SECRET_ACCESS_KEY`, `AWS_PROFILE`; adlfs `AZURE_STORAGE_CONNECTION_STRING`,
   `AZURE_STORAGE_ACCOUNT_NAME`/`AZURE_STORAGE_ACCOUNT_KEY`; gcsfs
   `GOOGLE_APPLICATION_CREDENTIALS`; SFTP keys in `~/.ssh` or an ssh-agent.
3. **Prefect Secret block.** `{{ prefect.blocks.secret.<name> }}` (Prefect's
   own placeholder syntax) in `url` and every string of `storage_options`. A
   value that is only the placeholder takes the secret's JSON value when it
   holds JSON, so `storage_options: "{{ prefect.blocks.secret.azure-archive }}"`
   supplies a whole mapping.
4. **Prefect RemoteFileSystem block.** `block: <name>` (instead of `url`)
   loads a `RemoteFileSystem` block: `basepath` becomes the `url` and
   `settings` the `storage_options`; inline `storage_options` are merged over
   them, then placeholders are expanded. Prefect stores `settings` as plain
   text, so keep secrets in Secret blocks and reference them from the
   settings with placeholders.

Placeholders are checked for syntax when the config is loaded. Blocks are
resolved lazily, when a storage location is used, by
`storage/credentials.py` (`resolve` gives the URL and options;
`open_location` builds the `StorageLocation`; `location_problems` lists the
problems and never raises), with a 60s cache per block (missing and failed
lookups are cached too), so a block created or changed after startup is picked
up without a restart. The Prefect lookups are a resolver the server registers
(`set_block_resolver`; `server/services/blocks.py`, installed after Prefect is
up and by flow-run processes); without one (unit tests, client) placeholders
are unresolvable. Other modules add problems for a resolved storage location
with `register_problem_check` (rclone uses it).

A whole-value placeholder takes the secret's value as JSON only when it is a
JSON object or array, so a password such as `12345` stays a string; every
string inside such a value is registered for redaction on its own. A
malformed placeholder is reported by field and expected syntax only, never by
value, because `${VAR}` has already been expanded into it. A
storage location configured with `block:` shows as `prefect-block:<name>` in
logs, messages and the DB `storage_location.url` column; a `url` with
placeholders is stored unresolved. Inline options are merged over the block's
settings key by key (nested mappings merge recursively).

Because that configured `url` does not change when a block's base path (or a
whole-URL Secret) does, a moved root is detected on open instead:
`stores.note_resolved_root` (called by `store_for` and the index's
`resolve_location`) compares the credential-free resolved root
(`credentials.root_identity`: scheme, host, port and path) with
`storage_location.resolved_root`. The first one is recorded; a different one
resets the storage location exactly as a changed `url` does
(`models.storage_location_moved`: copies pending re-check, generations
bumped, index progress dropped). A per-process cache skips the database while
the root is unchanged, and nothing is recorded while the blocks cannot be
resolved.

**A missing block never stops startup**, because the user needs the server
running to create it. The storage location is **incomplete** instead:

- the startup root check skips it with a warning naming the file, field and
  block (storage locations that use blocks are checked once Prefect is up);
- a run that uses it fails with the same message; other storage locations
  keep working;
- an API call that needs it returns `409` with
  `{detail, reason: "storage_location_incomplete", problems}`; the client
  transfers through the API (not directly) while a storage location is
  incomplete, so it gets that error too;
- the storage-locations API returns `status: ok | incomplete` and
  `problems: [str]`, and the UI's storage pages show a warning with the
  problems and a link to the Prefect blocks page.

A missing rclone config block, a Prefect server that cannot be reached and an
rclone binary that cannot be found or downloaded make a storage location
incomplete in the same way.

Resolved values are treated like `${VAR}` values: redacted in logs and errors
(STO-16; resolved Secret values are registered with `storage/redact.py`) and
returned only to `hp-client-storage-read-write` (`GET
/storage-locations/{name}` returns the resolved `url` and `storage_options`,
or the configured ones while incomplete).

## rclone

`rclone://<remote>/<path>` storage locations reach any
[rclone](https://rclone.org/) remote (SharePoint, OneDrive, Box, ...) through
an fsspec filesystem, `heliopuddle.storage.rclone.RcloneFileSystem`,
registered as the `rclone` protocol. It is a backend package like s3fs: it is
the only code besides `backends.py` that knows about rclone. It talks to a
local `rclone rcd` over HTTP (httpx).

### Configuration

`storage_options` takes exactly one config source:

- `config_block: <name>`: a Prefect block of the type prefect-managedfiletransfer
  defines, `RCloneConfigFileBlock` ("RClone Remote Config File
  [ManagedFileTransfer]", fields `remote_name` and `config_file_contents`).
  Existing blocks work unchanged; hp registers the same block type when that
  package is not installed and does not depend on it. `config_file_contents`
  is a plain string: anyone who can read Prefect blocks can read the tokens.
- `config_file: <path>`: an `rclone.conf` on this machine, for single-host
  setups.

and optionally `verify` (below). Make the remote with `rclone config` and paste
the file into the block. SharePoint is `type = onedrive` with
`drive_type = documentLibrary`; Box servers should prefer JWT
(`box_config_file`, `box_sub_type = enterprise`): Box documents JWT as
exchanging a newly signed assertion for each token and never mentions a
refresh token, whereas an OAuth refresh token "can be used ... only once".

### Sessions: one rcd per config, started locally

Every rclone call runs in an **rclone session**
(`heliopuddle/storage/rclone/session.py`):

1. take one slot of the Prefect global concurrency limit `rclone:<config>`
   (limit 1, created on first use; `<config>` is the block name or
   `file:<first 12 hex of the sha256 of the absolute path>`, since limit names cannot contain `/`) for every config the session needs;
2. write the configs into a private temp config (0600 in a 0700 directory;
   duplicate remote names are a config error) and start
   `rclone rcd --rc-serve` on a Unix socket in that directory, with a random
   user and password;
3. run the calls;
4. stop rcd, save each config whose contents changed (a refreshed OAuth
   token) back to its block or file, even after a failure, delete the temp
   directory and release the slots.

So across every process and node, at most one rcd uses a given config, and
refreshed tokens are never lost or raced (a Box OAuth refresh token is single
use). Different configs, normally different projects, run in parallel.

A process can hold several sessions, each over exactly the configs its first
caller asked for (never merged with other callers' configs, so two configs
that both define a remote such as `archive` never collide unless one caller
uses both). A session is shared by every caller in the process that needs a
subset of its configs (reference counted) and closed `HELIOPUDDLE_RCLONE_IDLE`
(default `2s`) after its last use, so a batch of calls pays for one rcd start.
A caller whose configs overlap no session opens its own, in parallel with the
others. Only overlapping configs wait: a caller needing a config that an
overlapping session lacks marks that session draining (no new callers join
it), waits for it to close and tries again. A thread already holding a
session that lacks a config it now needs gets an error at once instead of
waiting for itself. The slots are held by a session thread for the session's
life; Prefect renews their lease, and a dead holder's lease expires.

One deadline bounds every wait of a call: for an overlapping session to close,
for the slots (a new session's thread gets the caller's remaining time) and
for rcd to be ready. A caller out of time drops only its own reference: a
session other callers hold or are starting keeps going (a caller with a longer
budget whose shared session gave up on its slots tries again within its own
budget), and one nobody holds any more releases its slots without starting
rcd, or closes when idle.
Services wait for slots without limit; API requests wait at most
`HELIOPUDDLE_RCLONE_API_WAIT` (default `30s`) and then answer `503` with
`reason: rclone_busy` and `Retry-After`. `RcloneBusyError` is a `TimeoutError`
(an `OSError`), so the content, preview and upload services and routers
re-raise it before their generic storage-failure handlers. Throughput on one
config is serialised cluster-wide.

The lock and the block store are hooks the server registers
(`set_session_hooks`); without them (unit tests) a process-local lock and the
file config source are used.

`server start` registers the `RCloneConfigFileBlock` type with Prefect once
Prefect is up (`register_rclone_block_type`), so the block can be created in
the Prefect UI on a fresh server; a failure is a warning, never a startup
error. On shutdown it closes every open rclone session after the served
deployments stop and **before** Prefect stops (`close_rclone_sessions`),
because writing a refreshed token back to its block needs Prefect; a session
that does not close in time or a config that could not be saved is logged as
an error (the remote may need reconnecting).

A `config_file` is identified by its canonical path (`Path.resolve()`:
symlinks and `..` resolved), so two spellings of one file share one lock.

### Security

rcd's API can read and write anything rclone can reach, including every
configured remote's tokens, so:

- rcd listens only on a Unix socket in the session's 0700 temp directory
  (loopback TCP with the same auth where Unix sockets are unavailable, or
  where the socket path would be too long), with a random 32-byte user and
  password per session, never `--rc-no-auth`, `--rc-web-gui` or metrics. The
  user and password reach rcd as `RCLONE_RC_USER`/`RCLONE_RC_PASS` in its
  environment (readable only by its owner), not as `--rc-user`/`--rc-pass`
  (any local user can read a command line), and every inherited `RCLONE_*`
  variable is dropped, so none (such as `RCLONE_RC_NO_AUTH`) can loosen this;
- only remotes defined in the session's configs can be addressed (the remote
  name is checked; rclone's on-the-fly `:backend:` and local paths are not);
- hp calls only `operations/{list,stat,about,hashsum,copyfile,movefile,deletefile,mkdir,rmdir,purge}`,
  `config/listremotes`, `core/version`, `core/quit` and `--rc-serve` GETs;
- rcd runs in its own process group, is stopped (SIGTERM; `core/quit` where
  there are no signals) with the session and killed if hp exits (atexit, and
  a parent-death signal on Linux);
- the credentials and config contents never appear in logs, errors, the API
  or run parameters.

### Reads and writes

- Reads stream from `--rc-serve` (`GET /[remote:path]/file` with `Range`).
- Writes spool to a local file in a private (0700) temp directory while
  hashing it (md5); commit is `operations/copyfile` from the spool (rcd runs
  on the same machine), so the size is known and backends without streamed
  uploads (OneDrive/SharePoint) use their normal chunked upload with rclone's
  retries. The writer is a buffered (`AbstractBufferedFile`) writer, so
  `backends.begin_write` defers its commit like an object store's: a failing
  source never reaches the remote, abort deletes the spool, and `close()`
  after an abort cannot commit. If the copy or the verification fails, a
  destination the commit created is deleted.
- Neither a reader nor a writer holds a session between calls: each range
  read and each commit runs in its own session use (sessions stay open
  `idle` seconds), so streaming a large source does not hold the config lock,
  and a copy between two rclone configs in one thread cannot deadlock. A
  thread that already uses a session and then needs a config it lacks gets an
  error at once instead of waiting for itself.
- Copies and moves pass `IgnoreTimes`: rclone otherwise treats an existing
  destination of the same size and modification time as already transferred
  (a move then just deletes the source).
- `mv` is `operations/movefile`, which replaces an existing destination
  (`move_replaces` is true).
- Free space is `operations/about`'s `free` when the backend reports it,
  otherwise unknown (the `quota` setting applies).
- Listing is `operations/list`; modification time is `ModTime`; there is no
  md5 for Box or OneDrive, so the indexer reads content as for SFTP.

### Verify

Each rclone storage location has `verify: auto | native | content | size`
(default `auto`):

- `native`: compare the provider's own hash of the stored object
  (`operations/stat` with hashes: Box SHA1, OneDrive/SharePoint QuickXorHash)
  with the same hash of the local spool (`operations/hashsum`; for a remote
  that reports md5, the md5 computed while spooling). The first type the
  remote reports wins, in the order md5, sha1, quickxor, sha256, crc32c,
  dropbox, hidrive, mailru, whirlpool, crc32. No download. The size is checked
  too.
  QuickXorHash is not cryptographic, but it is computed by the provider from
  the stored bytes and catches corruption.
- `content`: read the destination back and compare md5 (the rule elsewhere).
- `size`: compare sizes only (the provider is asked for no hashes: some
  backends read the whole object to compute them).
- `auto`: `native` when the backend reports a hash type that the local
  backend can compute, else `content`.

hp still records the md5 (computed while spooling) as the file's hash. Each
process remembers, for the objects it wrote, the size, the native hash type
and value and the md5; `StorageLocation.md5` uses that md5 without reading
the object (`backends.verified_md5` → `fs.hp_verified_md5`) only while the
provider's hash (for `size`, the size) still matches; otherwise, and for
`content`, it reads the content. A record is reused only as far as the
*reading* storage location's `verify` allows, whichever mode wrote it:
`content` never reuses one, `native` and `auto` reuse only a native-hash
record, and `size` also reuses a size-only record. A
provider-computed hash of the stored bytes counts as verifying the
destination hash for STO-03. SharePoint changes some uploaded files (mainly
Office documents); `native` and `content` both detect it, and the transfer is
not committed, so the source is kept.

### The binary

The Docker image contains a pinned rclone. Elsewhere hp uses
`HELIOPUDDLE_RCLONE_BINARY`, then `rclone` on `PATH`, and otherwise downloads
the pinned release (`heliopuddle/storage/rclone/binary.py`) from
`https://downloads.rclone.org/v<ver>/rclone-v<ver>-<os>-<arch>.zip` into
`<data dir>/bin/rclone-v<ver>/`, checking its SHA256 against values pinned
in that module (rclone's `SHA256SUMS` are PGP-signed; pinning them avoids
needing gpg). `HELIOPUDDLE_RCLONE_AUTO_DOWNLOAD=false` disables it. A failure
makes the rclone storage locations incomplete.

### A storage location that holds one folder

See [Partial storage locations](#partial-storage-locations-path_prefix): an
rclone mirror of one project folder is the typical case.

### Constraints

- An rclone storage location cannot be a project's first storage location
  (it would put every upload and download on the config lock). A literal
  `rclone://` URL is a config error; a first storage location whose block or
  whole-URL Secret resolves to one is incomplete with that problem, checked
  on every resolution, so a block created after startup is caught too.
- Clients never reach rcd: the API reports `direct_access: false` and clients
  transfer through the API. The flag is decided from the resolved URL (only
  the flag is returned); a storage location that cannot be resolved reports
  `false`.

### Tests

There is no emulator for Box or SharePoint. The conformance suite runs through
rclone's `webdav` backend against a WebDAV server container (no md5 and no
streamed upload, like OneDrive), and token write-back is tested with an OAuth
remote whose `token_url` points at a fake local token endpoint. Spike results (rclone v1.76.0):
Unix-socket rc with httpx's UDS transport works and rejects requests without
credentials (`401`); `operations/copyfile` from a local absolute path and
`operations/hashsum` with `quickxor` or `sha1` on a local file work;
`--rc-serve` honours `Range` (`206`); `operations/movefile` replaces the
destination; `operations/about` fails with an error on backends without it.

## Partial storage locations (`path_prefix`)

A storage location may hold one project folder rather than the whole
project (STO-23). With `path_prefix: upload_mirror`, its root holds what the
project calls `upload_mirror/`: the project path `upload_mirror/x.csv` is
stored at `<root>/x.csv`, and no path outside `upload_mirror/` can have a copy
there. Typical use: `storageA` (a share) holds the whole project and
`storageB` (an rclone remote) has its root at the equivalent of
`storageA/upload_mirror`, filled by a rule copying `upload_mirror/**`.

The mapping lives only in `StorageLocation` (helpers in
`storage/paths.py`), so it works for every backend and nothing else changes:

- every path argument is a project path; `full_path` strips the prefix
  (`strip_path_prefix`) and raises `PathOutsideStorageLocation`, a
  `ValueError`, for a path outside it; `covers(path)` asks without raising;
- `list_files(prefix)` takes a project listing prefix and yields project
  paths (`<prefix>/<rel>`); a listing prefix that is the folder or one of its
  parents lists the whole root (`listing_scope`), one outside it lists
  nothing, so the index adopts what it finds as `upload_mirror/...` and
  `index.incremental_prefixes` keep working;
- staging names (`<path>.<token>.hp-tmp`) are project paths too, so they land
  next to the file inside the root.

Around it:

- **Rules** pass the destination's prefix to `select_candidates`
  (`within`), so a file outside it is never selected, counted or retried.
- **Uploads and registration** refuse a path outside the target storage
  location's prefix with `422` before anything is written
  (`api.errors.require_covered`).
- **Clients** get `path_prefix` in the storage-location record and pass it
  to `StorageLocation` for direct access.
- **Identity:** `storage_location.path_prefix` (migration 0003) is compared
  at config sync; a change moves every copy, so they are reset as for a
  changed `url` (SVC-12).
- The prefix is validated when the config loads: relative, `/`-separated,
  with no empty, `.` or `..` segment, no `\` and no glob character.

## Backend-specific knowledge is isolated

Everything hp knows about particular backends is in one module,
`heliopuddle/storage/backends.py`, and nowhere else (a unit test fails
otherwise):

1. **Which protocols are object stores**, used for the default of
   `create_root` (below) and to know that only an object store's bucket or
   container (the root's first path segment) has to exist, its prefixes being
   implicit (`root_anchor`).
2. **How to read a backend-reported md5** from `info` metadata (`md5`,
   `content_md5`/`content_settings.content_md5`, GCS `md5Hash`, S3 single-part
   `ETag`), used only as an indexing shortcut (`native_md5`).
3. **Which `info` keys hold a modification time** (`mtime`, `last_modified`,
   `LastModified`, `updated`, `created`; a datetime, a POSIX timestamp or an
   ISO 8601 string) (`info_mtime`).

4. **Whether `mv` replaces an existing destination** (`move_replaces`), used
   by the overwrite publish step ([operations](operations.md#overwrite)).
   Local, memory and object stores replace, and so does SFTP (fsspec's `mv`
   is OpenSSH's atomic `posix-rename@openssh.com`); SMB does not
   (`NON_REPLACING_MOVE_PROTOCOLS`).
5. **How to write so that a failed source never commits** (`begin_write`,
   `finish_write`, `abort_write`; see [Writes](#writes)).
6. **Native free space** (`free_space(fs, path) -> int | None`): local via
   `shutil.disk_usage`, SMB via a volume-size query, SFTP via the
   `statvfs@openssh.com` extension, rclone through the filesystem's own
   `hp_free_space` (`operations/about`); unknown/unlimited backends return None.
   Unexpected failures are reported, not silently treated as unlimited.
7. **Same-filesystem copy capability** (`same_filesystem`): only confirmed
   compatible connections use generic `fs.copy`; all other transfers stream.
8. **An md5 the backend vouches for** (`verified_md5`): the filesystem's own
   `hp_verified_md5` (rclone, see [Verify](#verify)), else None.

It also registers the `rclone` protocol by import path (besides the
`fsspec.specs` entry point), so it resolves without a reinstall. hp's
`heliopuddle/storage/rclone/` package is a backend package like s3fs and is
exempt from the scan.

Adding support for a new backend never needs changes elsewhere; at most a line
in that module.

## Writes

New files at a path with no object are written directly to the final path; the
upload reservation guarantees no other hp operation touches that path
meanwhile. Overwrites, and new uploads over an existing object, are staged to
a unique `<path>.<token>.hp-tmp` and then published (see
[operations](operations.md)): hp never writes directly over an existing
object. The index never indexes a `*.hp-tmp` object; the full index sweeps
those no operation owns (matched by their exact `<path>.<uuid>.hp-tmp` /
`<path>.<uuid>.hp-old.hp-tmp` shape).

`StorageLocation` has three write calls:

- `write(path, source)` writes `path` directly (the caller chooses the final
  or a staging path);
- `write_staged(path, token, source)` writes `staging_path(path, token)`
  (`<path>.<token>.hp-tmp`) and returns it. A token is unique per attempt, so
  concurrent attempts never share a file; on failure only that attempt's
  staging object is removed, never any other `*.hp-tmp` name, and the final
  path is never touched;
- `move(src, dst)` is the generic `fs.mv`; `move_replaces` says whether it
  replaces an existing `dst` on this backend.

The optional `write(..., check=...)` callback lets a server transfer recheck
its destination ownership before commit and before removing partial bytes.
Transfers also check while streaming; a stale writer must not clean up an
object belonging to the operation which took its path over. Native-copy
failures use the same guarded partial-object cleanup.

**A write whose source fails part-way is aborted, never committed.** The rule
is never to `close()` a file object whose source failed: in fsspec `close()`
commits, and `with` and `__del__` both call it. The helpers in `backends.py`:

- `begin_write(fs, path)` opens `"wb"`. A buffered writer
  (`fsspec.spec.AbstractBufferedFile`, which the object-store writers are) has
  `autocommit` set to False before any byte is written, so nothing is
  published until `finish_write`. Other file objects (local, memory, SFTP,
  SMB) write in place; they are not opened with `autocommit=False`, because
  those backends would then write to a temporary name of their own, outside
  the `.hp-tmp` convention.
- `finish_write(fobj)` closes, then commits a deferred writer.
- `abort_write(fobj)` (best effort, never raises): for a buffered writer,
  `discard()`, then the file object is marked closed and its buffer dropped,
  so neither `close()` nor its finaliser can commit; any other file object is
  closed to release its handle.

On a failure in the copy or in `finish_write`, `write` calls `abort_write`
and, for an in-place writer, removes the partial file if the path did not
exist before. Per backend:

| Backend | On a failed write |
|---|---|
| S3 | the multipart upload is aborted (`discard()`); a small write never left the buffer. The existing object is unchanged. |
| GCS | the resumable upload is cancelled (`discard()`). The existing object is unchanged. |
| Azure Blob | staged blocks are never committed (the block list is committed only on close) and expire on the service side. The existing blob is unchanged. |
| local, memory, SFTP, SMB | the file was written in place: a partial new file is removed. Overwrites never write the final path directly (they are staged), so an existing file is unchanged. |

The configured `create_root` value is also returned by the storage-locations
API, so clients behave the same way as the server.

### Roots

`create_root` is set per storage location: `auto` (default), `true` or
`false`.

- `auto`: directory-style backends (local disk, SMB, SFTP and any other
  non-object store) have a missing root created. Object stores (S3, Azure,
  GCS) do not: creating a bucket or container needs extra cloud permissions
  and choices such as region, so it is left to you and a missing one is a
  clear error.
- `true`: always try to create the root, including a bucket or container.
- `false`: never create it.

`StorageLocation.ensure_root()` does the check and the creation. A root is
created with one generic call, `fs.mkdir(<root anchor>, create_parents=True)`
(a `FileExistsError` is ignored), on the root anchor (`backends.root_anchor`:
the root directory itself, or on an object store the bucket or container).
`mkdir` is used rather than `makedirs` because some object-store backends
implement `makedirs` as a no-op, while their `mkdir` with `create_parents`
creates the bucket or container (verified on Azurite). The root is then
checked again; if it still does not exist, or creating it raised, the result is
`StorageRootMissingError` with the reason.

Roots are checked (and created per the setting) **at startup** for every
active storage location (every one in the project config), after the config
is synced into the database and before anything is served; a root that is
missing and cannot be created stops startup (exit code 2) with a message
naming the project, storage location and URL. Before every write
the root is checked again with `fs.exists` (on an object store the bucket or
container, elsewhere the root directory) and created per the setting, so a
root removed later is recreated or reported. Missing directories below the
root are created with `fs.makedirs(parent, exist_ok=True)` (a no-op on most
object stores; the root check comes first because some backends' `makedirs`
would create a missing bucket). A missing root raises `StorageRootMissingError`
(a `FileNotFoundError`) naming the storage location URL. Listing a missing root
yields nothing.

The only backend-specific input is `backends.is_object_store`.

## Hashing

hp identifies content by md5, computed by hp in Python by streaming the file
(8 MiB chunks). It is a comparison hash: equal md5 and size means the same
content in any storage location. `file.hash_algo` records the algorithm so it
can change later.

A backend-reported md5 is metadata that any writer can set, and most backends
don't have one for every object (Azure only for single-shot uploads, S3 only
for single-part uploads, GCS not for composite objects, SFTP/SMB/local never).
So:

- for rclone storage locations, transfers verify per the storage location's
  `verify` setting ([rclone](#verify));
- the **indexer** may use a backend-reported md5 to avoid reading an object
  when adopting or re-indexing;
- **verify** and **accept** read the content (except rclone `native`/`size`).

## Listing

`StorageLocation.list_files(prefix)` streams one directory at a time through
`fs.ls(..., detail=True)` and yields relative paths with size, mtime
(`backends.info_mtime`) and the backend-reported md5 if any
(`backends.native_md5`). It neither materialises nor sorts the whole tree;
a missing prefix yields nothing.

### Symlinks and credentials (STO-15, STO-16)

On the local filesystem a link planted inside a root could point at a file
outside it (for example the auth secret). `backends.is_link` and
`backends.path_has_symlink` (the only code that knows which protocols have
links) let `StorageLocation` skip symlinked files and directories when listing
and raise `SymlinkError` for any read, write or move whose path is, or lies below,
a link inside the root. The root itself may be a link. Other backends return
False at no cost.

A storage URL may embed a password (`sftp://user:pass@host/path`), but
**credentials belong in `storage_options`**, written as `${ENV}` references so
the YAML holds no secret. Whatever the URL, `storage/redact.py` (`redact_url`,
`redact_text`) hides the password (`sftp://user:***@host/path`) and secret query
values such as `sig=` in every exception message and log line that formats a
storage URL, and in backend error text quoted in startup errors.

## Tests

### Free space and quotas (M4)

An optional storage-location `quota`, such as `500GB`, applies to every
backend. Quota space is quota minus the indexed size of non-deleted copies in
that storage location. When both quota and a native value exist, the smaller
wins. Object stores without quotas have no space limit. Rule transfers check
file size plus `check_for_space_overhead` (default `2GB`) unless
`check_for_space` is false. Units such as `GB` are decimal; `GiB` is binary.
The location API exposes quota with the other validated config.

Native free-space behavior is tested with local storage and fakes for each
supported backend; quota behavior uses the shared conformance tests, including
Azurite. Transfer safety is independent of whether bytes were streamed or
copied within one filesystem: destination read-back verification is mandatory.

Local queries use the nearest existing ancestor when the root has not yet
been created. The SMB volume query uses the caller-available byte count, not
the volume's unrestricted free count. SFTP sends `statvfs@openssh.com` and
uses `f_bavail * f_frsize` (falling back to `f_bsize` when needed). A failed or
unsupported native query fails the space check; it never silently disables
the limit. `StorageLocation.free_space(quota=..., indexed_size=...)` combines
the native result and quota, flooring exhausted quotas at zero.

Same-filesystem copying requires matching implementation and connection
identity, plus an implemented generic copy operation. A shared protocol alone
is insufficient; different accounts or servers stream. Backends without an
implemented copy operation also stream on the same connection.

### Shared conformance suite

One conformance suite (`tests/storage_conformance.py`) runs unchanged against
every test backend: memory and local in the unit suite, Azure (Azurite) — the
canonical cloud backend — in the integration suite. It covers direct and
staged writes (unique staging paths), abort on a source failure, and both
move semantics: the move test follows `move_replaces`, and a test-only
memory filesystem whose `mv` refuses an existing destination exercises the
non-replacing branch in the unit suite. A test module subclasses
`StorageConformance` and provides two fixtures: `location_factory()` (a fresh,
empty root that exists) and `missing_root_factory(create_root)` (a root, or
bucket/container, that does not), so a new backend is tested by adding one
module with those fixtures. The object-store abort is also tested against the
real s3fs writer with its network calls intercepted
(`tests/unit/test_write_abort.py`).

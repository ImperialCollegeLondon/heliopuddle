# Storage and operations requirements (`STO`)

**STO-01 (M1)** The storage-location wrapper `StorageLocation(url, storage_options)`
provides `open_read`, `write` (direct; staged writes use a unique `.hp-tmp`
path, see [operations](../../design/operations.md)), `md5` (streamed, 8 MiB chunks), `info` (size, mtime), `exists`,
`rm` and `list_files`. Tested on `file://` and `memory://`.
*Verify:* unit tests on `file://` and `memory://`.

**STO-02 (M3)** Azure Blob is the canonical cloud backend for the slow
integration tests: the STO-07 conformance suite runs against Azurite
(`mcr.microsoft.com/azure-storage/azurite`), as do indexing tests and the
Docker e2e test (DEP-03). Nothing in hp is specific to Azure apart from the
isolated table in `storage/backends.py` (STO-06).
*Verify:* integration tests against Azurite.

**STO-06 (M3)** Storage is generic over fsspec. `StorageLocation`, the client,
the services and the indexer use only the generic fsspec filesystem API
(`open`, `info`, `ls`, `exists`, `rm`, `mv`, `makedirs`, `copy`). The only
backend-specific knowledge — which protocols are object stores (for
`create_root: auto`), whether `mv` replaces its destination, and how to read a backend-reported md5 from `info`
metadata, native free-space queries and same-filesystem copy capability —
lives in one module, `heliopuddle/storage/backends.py`. Supported
out of the box through fsspec and the `heliopuddle[all]` extras: local
(`file://`), SMB (`smb://`), SFTP (`sftp://`), S3 (`s3://`), Azure Blob
(`az://`, `abfs://`) and GCS (`gs://`, `gcs://`). Any other fsspec backend works
once its package is installed. Creating a missing root follows STO-08.
*Verify:* a unit test fails if a protocol name or backend package appears in
`src/` outside `storage/backends.py`; a smoke test that each of the six target
protocols resolves to an importable fsspec filesystem class.

**STO-07 (M3)** One storage conformance suite runs unchanged against every test
backend: direct and staged writes (unique staging paths, abort on source
failure, replacing and non-replacing moves), read, md5, `info`, `exists`,
`rm`, recursive `list_files`, a missing prefix, a missing root (with
`create_root: false` a clear error and nothing created; `auto`/`true` as STO-08
says for the backend type) and paths outside the root rejected.
*Verify:* memory and local in unit tests; Azurite in integration tests.

**STO-08 (M3)** Each storage location has `create_root: auto | true | false`
(default `auto`). With `auto` a missing root is created for directory-style
backends (local, SMB, SFTP, any non-object store) but not for object stores
(S3, Azure, GCS); `true` always tries to create it; `false` never does. Roots
are checked and created at startup for every active storage location, and a
missing root that cannot be created stops startup with a message naming the
storage location and URL. A root that goes missing later is recreated per the
setting on the next write, or raises `StorageRootMissingError`. Only
`backends.is_object_store` decides `auto` (STO-06 holds).
*Verify:* unit tests on local/memory (auto creates, false errors, recreated on
write) and a fake object store (auto does not create); CLI start fails fast;
Azurite: `auto` does not create a container, `true` does.

**STO-09 (M3)** Path ownership ([operations](../../design/operations.md)). One operation at a time owns a
(project, path, storage location) through the copy state plus an `op_token`
and `op_deadline`: a new-file upload (`pending` with a token), an overwrite
(`overwriting`) or a delete (`deleting`). Other operations get `409 busy`; the
index skips owned paths. New files are written straight to the final path by
the reservation holder only, unless an object already exists there, when the
write is staged and published like an overwrite (an existing object is never
written over). Reserve, renew, abort and complete endpoints
exist; expired reservations are repaired without removing content that
matches the reserved hash.
*Verify:* deterministic phase-interleaving tests: two clients on an absent path
→ exactly one owns it and the winner's bytes and record survive; expiry and
takeover; crash before complete. Assertions cover store and DB state.

**STO-10 (M3)** Overwrite publication ([operations](../../design/operations.md)): at every step either the complete
original or the complete verified new content is at the final path; replacing
and non-replacing moves follow `backends.move_replaces`; orphaned staged and
backup files are repaired by the full index; on failure the copy returns to
`available` with the original.
*Verify:* fault injection at each publish step on a replacing and a
non-replacing fake filesystem and on Azurite; invariant: final hash ∈ {old,
new} and DB hash = store hash.

**STO-11 (M3)** Every staged write uses a unique `<path>.<id>.hp-tmp`; only its
owner cleans it up.
*Verify:* concurrent identical staged writes both behave correctly; no
cross-cleanup.

**STO-12 (M3)** A source failure part-way through a write never commits partial
bytes over an existing object; partial new-file uploads never become
`available` and are cleaned up.
*Verify:* a source that yields some bytes then raises, against a real s3fs
writer with network calls intercepted and against memory/local; the existing
final object is unchanged.

**STO-13 (M3)** A delete removes a store object only if it matches hp's last
observation of that copy, guarded by generation; a stale delete never removes
newer content.
*Verify:* interleaving tests (delete vs re-upload); storage-URL-change
scenario.

**STO-03 (M4)** A move never deletes the source before the destination hash
is verified. It snapshots source generation/hash and a changed source is
never deleted.
*Verify:* fault injection at each step (space check, copy, verify, mark
available, source delete); every pre-delete failure leaves the source intact
and its state unchanged. A failed store removal retains the source and uses
the guarded delete's incomplete state. Cover source changes during transfer,
renewal failure, changed endpoint URLs and repair after an interrupted move.

**STO-04 (M4)** Same-hash destinations are verified and marked available
without writing. Different hashes: skip a newer destination when
`update_only_if_newer`; otherwise replace with `overwrite`, else mark unknown
and report a skip. `check_for_space` (default true) requires source size plus
overhead (default 2GB), taking the smaller native/quota value.
*Verify:* table-driven unit tests.

**STO-14 (M4)** `backends.free_space(fs, path)` reports native free space for
local (`disk_usage`), SMB (volume-size query) and SFTP (`statvfs@openssh.com`).
Optional storage-location quota works on any backend: quota minus indexed
size. Object stores without quota are unlimited.
*Verify:* shared conformance tests on local and fake backends, plus Azurite
with quota.

**STO-15 (M6A)** **Symlinks are never followed.** Listing a local storage
location skips symlinked files and directories; reading, writing or moving a path
that is, or lies below, a symlink inside the root raises `SymlinkError`. The root
itself may be a symlink. Only `backends.py` knows which protocols have links.
*Verify:* unit tests with links to a file and a directory outside the root.

**STO-16 (M6A)** **Storage URLs are redacted.** The password in a storage URL's
user info (and secret query values) is replaced by `***` in every exception message
and log line that formats the URL, including startup errors and backend error text
quoted in them. Credentials belong in `storage_options` (`${ENV}`).
*Verify:* unit tests of `redact_url`, `redact_text`, the root-missing error and the
startup error.

**STO-05 (M7)** **rclone storage locations.** `rclone://<remote>/<path>`
is an fsspec backend (`heliopuddle.storage.rclone`) over a local `rclone rcd`
(STO-20). Reads stream with HTTP Range from `--rc-serve`. Writes spool to a
local file (computing md5) and commit with `operations/copyfile` from the
spool, so a failing source never reaches the remote. `mv` replaces an existing
destination. Free space comes from `operations/about` when the backend
reports it, otherwise it is unknown (the storage-location `quota` applies).
Each rclone storage location has `verify: auto | native | content | size`
(default `auto`): `native` compares the provider's own hash of the stored
object (`operations/stat` with hashes) with the same hash of the local spool
(`operations/hashsum`); `content` reads the destination back (md5); `size`
compares sizes; `auto` is `native` when the backend reports a hash and
`content` otherwise. A provider-computed hash of the stored bytes counts as
verifying the destination hash (STO-03). An rclone storage location cannot be
a project's first storage location (a literal URL is a config error; a block
or Secret that resolves to one makes the storage location incomplete) and is
never accessed directly by clients (`direct_access: false` in the API, decided
from the resolved URL and `false` while it cannot be resolved).
*Verify:* the STO-07 conformance suite through rclone's webdav backend against
a WebDAV container (integration); unit tests against a fake rc server for
spool, abort, verify modes, `about` fallbacks and errors.

**STO-17 (M7)** **SFTP through fsspec.** `sftp://` storage locations use
fsspec's `SFTPFileSystem` with no SFTP-specific code beyond the tables in
`storage/backends.py`.
*Verify:* the STO-07 conformance suite against an OpenSSH server container
(integration), plus replacing moves (`posix-rename`), `statvfs` free space, `create_root:
auto` creating the root, and key and password authentication.

**STO-18 (M7)** **Credentials from config, environment or Prefect.** In a
storage location's `url` and every string of its `storage_options`:
literal values, `${VAR}`/`${VAR:-default}` (resolved at load; an unset
variable stops startup, as before), and `{{ prefect.blocks.secret.<name> }}`
(a Prefect Secret block). A value that is only the placeholder takes the
secret's JSON value when it holds JSON (so a whole `storage_options` mapping
can come from one block). Blocks are resolved lazily when a storage location
is used and cached for 60s, so a block created after startup works without a
restart. A missing block, or Prefect being unreachable, never stops startup:
the storage location is `incomplete`, the startup root check skips it with a
warning, runs that use it fail with a message naming the file, field and
block, API calls that need it return `409 storage_location_incomplete`, the
storage-locations API returns `status: incomplete` and `problems`, and the UI
storage pages show a warning. Resolved values are redacted like the URL
(STO-16).
*Verify:* unit tests with a fake block resolver (expansion, JSON, cache expiry,
incomplete status, 409, UI warning); integration test on the Prefect harness:
start without the block, see the warning, create it, use the storage location
without a restart.

**STO-19 (M7)** **Prefect RemoteFileSystem blocks.** `block: <name>` on a storage
location loads a Prefect `RemoteFileSystem` block: `basepath` is the `url` and
`settings` the `storage_options`. Inline `storage_options` are merged over the
block's settings and placeholders are then expanded. `block` with `url` is a
config error. A missing block marks the storage location `incomplete`
(STO-18). When the root a block (or a whole-URL Secret) resolves to changes,
even after startup, the storage location is reset as for a changed `url`
(SVC-12): copies pending re-check, generations bumped, index progress
dropped. The comparison uses the credential-free root stored in
`storage_location.resolved_root`.
*Verify:* unit tests with a fake resolver (including a moved root resetting the
copies); integration test with a real block.

**STO-20 (M7)** **One rcd per rclone config, started locally.** Every
interaction with an rclone storage location runs inside an rclone session:
hold one slot of the Prefect global concurrency limit `rclone:<config>`
(limit 1; `<config>` is the config block name or `file:<first 12 hex of the sha256 of the absolute path>`, since limit names cannot contain `/`) for every
config it uses, start `rclone rcd` on this machine with a private temp config
assembled from those configs, use it, stop it, save changed config back and
release. So across all processes and nodes only one rcd uses a given config
at a time, and different configs (normally different projects) run in
parallel. The config comes from `storage_options.config_block` (a
prefect-managedfiletransfer `RCloneConfigFileBlock` document: `remote_name`,
`config_file_contents`) or `storage_options.config_file`; after every session,
including failed ones, a config whose contents changed (a refreshed token) is
saved back to its block or file. Sessions are re-entrant within a process.
A process can run several sessions at once, each over exactly the configs
its caller asked for (unrelated configs are never merged); only a caller
whose configs overlap an open session lacking some of them waits for that
session to close. Flow tasks hold a session per batch; API requests wait at
most `HELIOPUDDLE_RCLONE_API_WAIT` (default `30s`), one deadline for every
wait (another session, the lock and rcd start), and then return `503` with
`Retry-After`; a request that gives up never stops a session other callers
use. A missing config block marks the storage location `incomplete`.
*Verify:* unit tests (session lifecycle, write-back on failure, re-entrancy,
config assembly, duplicate remote names, parallel sessions, one deadline);
integration test with two processes
on one external Prefect: rcd lifetimes on one config never overlap, on two
configs they can.

**STO-21 (M7)** **Token write-back with a real rclone.** An OAuth remote whose
token has expired and whose `token_url` points at a fake local token endpoint
is refreshed by rclone during a session, and the new token is in the config
block after the session, even though the backend call itself fails.
*Verify:* integration test with a fake local token endpoint, a Prefect block
and the rclone binary.

**STO-23 (M7)** **A storage location can hold one project folder.**
`path_prefix: <folder>` (default empty: the whole project) says that the
storage location's root holds the project folder `<folder>`: the project path
`<folder>/x.csv` is stored at `<root>/x.csv`, and a path outside the folder can
never have a copy there. The mapping is in `StorageLocation` alone, so it
applies to every backend (fsspec or rclone), and every path in the API, the
DB, rules and listings stays a project path. The prefix is a plain relative
folder (no `..`, `.`, empty segments, `\` or glob characters). A rule never
selects a file outside its destination's prefix; an upload or registration
outside a storage location's prefix is refused with `422`; the index adopts
files found under the root as `<folder>/...`; the API reports `path_prefix`
and the client applies it for direct access. A changed prefix moves every
copy, like a changed `url` (SVC-12).
*Verify:* unit tests of the mapping, listing and validation on local and
memory storage; a rule copying `upload_mirror/**` into a prefixed local and a
prefixed rclone (stand-in) storage location; index adoption; the 422; the
config-sync reset; integration test with the real rclone binary over WebDAV.

**STO-22 (M7)** **rcd is private.** rcd listens on a Unix socket in a 0700
temp directory (loopback TCP only where Unix sockets are unavailable), always
with a random per-session user and password, never with `--rc-no-auth`,
`--rc-web-gui` or metrics. hp calls only an allowlist of rc endpoints
(`operations/{list,stat,about,hashsum,copyfile,movefile,deletefile,mkdir,rmdir,purge}`,
`config/listremotes`, `core/version`, `core/quit` and `--rc-serve` reads). The
temp config is created 0600 and removed after the session. rcd is stopped
with the session and killed if hp dies. rcd credentials and config contents
never appear in logs, errors, the API or run parameters.
*Verify:* unit tests (allowlist, redaction, file modes, kill on exit);
integration test that a request without credentials gets `401`.

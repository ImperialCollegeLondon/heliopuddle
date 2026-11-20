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

**STO-05 (M7)** SFTP and rclone sync targets mirror a subset of files,
including deletes, using the bundled rclone binary, and write refreshed
tokens back to their config.
*Verify:* integration tests with an SFTP container and rclone's local/memory
backend.

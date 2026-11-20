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
| SFTP | `sftp://host/path` | paramiko (`sftp`) |
| S3 and S3-compatible | `s3://bucket/prefix` | s3fs (`s3`) |
| Azure Blob / ADLS | `az://container/prefix`, `abfs://...` | adlfs (`azure`) |
| Google Cloud Storage | `gs://bucket/prefix` | gcsfs (`gcs`) |

Any other fsspec implementation works once its package is installed; nothing
in hp needs to change.

Credentials and connection settings go in the storage location's
`storage_options`, passed straight to fsspec, with secrets from env vars:

```yaml
storage_locations:
  - name: primary
    url: s3://my-bucket/project
    storage_options: { key: "${AWS_ACCESS_KEY_ID}", secret: "${AWS_SECRET_ACCESS_KEY}" }
  - name: share
    url: smb://fileserver/data/project
    storage_options: { username: "${SMB_USER}", password: "${SMB_PASSWORD}" }
  - name: remote
    url: sftp://data.example.org/srv/project
    storage_options: { username: "${SFTP_USER}", key_filename: /config/id_ed25519 }
  - name: azure
    url: az://archive/project
    storage_options: { connection_string: "${AZURE_STORAGE_CONNECTION_STRING}" }
  - name: gcs
    url: gs://my-bucket/project
    storage_options: { token: /config/gcs-service-account.json }
```

Administrators receive the same URL and storage options from the API and write
to the store directly, so a client needs the same backend package installed
(`pip install heliopuddle[s3]`, ...).
In `local` and `oidc` auth modes these details require
`hp-client-storage-read-write` on the project (or `hp-admin`); storage-location
lists withhold the URL without that role. Ordinary readers/writers transfer
content through the API without receiving server credentials.
Trusted-proxy `header` mode retains direct access. See the
[auth security restriction](auth.md#backend-credentials-and-direct-file-access).

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
   Local, memory and object stores replace; SMB and SFTP do not
   (`NON_REPLACING_MOVE_PROTOCOLS`).
5. **How to write so that a failed source never commits** (`begin_write`,
   `finish_write`, `abort_write`; see [Writes](#writes)).
6. **Native free space** (`free_space(fs, path) -> int | None`): local via
   `shutil.disk_usage`, SMB via a volume-size query, SFTP via the
   `statvfs@openssh.com` extension; unknown/unlimited backends return None.
   Unexpected failures are reported, not silently treated as unlimited.
7. **Same-filesystem copy capability** (`same_filesystem`): only confirmed
   compatible connections use generic `fs.copy`; all other transfers stream.

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

- the **indexer** may use a backend-reported md5 to avoid reading an object
  when adopting or re-indexing;
- **verify** and **accept** always read the content.

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

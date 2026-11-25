# M7 plan (approved): SFTP, storage credentials and rclone storage locations

## Context

M7 was "Sync targets: SFTP and rclone", with SFTP via paramiko, rclone via a
bundled binary, and new `mirror` rules. The owner's review decided:

- **SFTP already works through fsspec.** `sftp://` is fsspec's own
  `SFTPFileSystem`, and paramiko is only its dependency, installed by the
  `heliopuddle[sftp]` extra. `backends.py` already handles SFTP's
  non-replacing `mv` and free space (`statvfs@openssh.com`), and the STO-06
  smoke test resolves `sftp`. No paramiko-specific work is needed. The gaps
  are a real-server test and user docs.
- **Credentials** come from the environment, inline, or a Prefect block.
- **Mirror rules are dropped.** The existing copy, move and delete rules
  already do this.
- **rclone** is a new fsspec backend, `rclone://`, so services, rules, the
  indexer and verification stay generic (STO-06).
  - It talks to an `rclone rcd` started locally, when and where it is needed.
  - A Prefect global concurrency limit per rclone config (normally one per
    project) makes sure **only one rcd uses a given config across the whole
    cluster** at any time. There is no rcd container or service.
  - The rclone config is kept the way prefect-managedfiletransfer keeps it, in
    a Prefect block, with refreshed tokens saved back.
  - Confidence for Box and SharePoint comes from an existing production pipeline, which
    already uploads to SharePoint through rclone with that block pattern.
    Tests use emulated backends only.

M7 is renamed "Storage locations: SFTP, credentials and rclone".

## 1. SFTP (STO-17)

- `tests/integration/test_storage_conformance_sftp.py` runs the existing
  `tests/storage_conformance.py` suite against an OpenSSH testcontainer.
  Extra tests cover:
  - the non-replacing move;
  - `statvfs` free space;
  - `create_root: auto` creating the root;
  - key auth and password auth.
- Docs: `heliopuddle[sftp]` is described as "fsspec's SFTP dependency
  (paramiko)" (DEP-12 amended). All "SFTP (paramiko) support" wording is
  removed from features.md, README and CLAUDE.md.

## 2. Credentials (STO-18, STO-19)

There are four routes, all documented in a new `docs/site/storage.md` with a
worked example for each backend:

1. **Inline** literal values in `storage_options`. This works today; the docs
   advise against it for secrets.
2. **Environment.**
   - `${VAR}` works today.
   - Backends also read their own variables and files: s3fs `AWS_*`, adlfs
     `AZURE_STORAGE_*`, gcsfs `GOOGLE_APPLICATION_CREDENTIALS`, SFTP keys in
     `~/.ssh` or an ssh-agent. This part is documentation only.
3. **Prefect Secret block.** `{{ prefect.blocks.secret.<name> }}` is Prefect's
   own placeholder syntax. It is expanded in `url` and in any
   `storage_options` string. When the whole value is the placeholder and the
   Secret holds JSON, the JSON becomes the value, so
   `storage_options: "{{ prefect.blocks.secret.azure-archive }}"` works.
4. **Prefect RemoteFileSystem block.**
   - `block: <name>` maps `basepath` to `url` and `settings` to
     `storage_options`.
   - Inline options are merged over the block's settings, then placeholders
     are expanded.
   - Prefect itself says `settings` "are stored as plain text … avoid storing
     credentials here", so secrets stay in Secret blocks.

**Blocks never block startup**, because the user needs the server running to
create them:

- Config loading checks only the placeholder syntax; `${VAR}` stays
  fail-fast, as today.
- `storage/credentials.py:resolve_location` resolves blocks lazily, whenever a
  `StorageLocation` is built for use, and caches the result for 60s. A block
  created later is therefore picked up without a restart.
- A storage location with a missing block, a missing rclone config, or a
  Prefect that cannot be reached is **incomplete**:
  - the startup root check skips it with a logged warning naming the file,
    field and block;
  - runs touching it fail with the same message, while other locations keep
    working;
  - API calls return `409 storage_location_incomplete`.
- The storage-locations API gains `status: ok | incomplete` and
  `problems: [str]`. The UI storage-location page and the storage list show a
  warning banner with the problems and a link to Prefect's blocks page.
- Resolved values are handled like `${VAR}` values: redacted (STO-16), and
  returned only to `hp-client-storage-read-write`.

## 3. rclone storage locations

### Config: the prefect-managedfiletransfer block pattern

- prefect-managedfiletransfer's `RCloneConfigFileBlock` has `remote_name` and
  `config_file_contents`. Its block type name is "RClone Remote Config File
  [ManagedFileTransfer]".
- The package writes the contents to a temp `--config` file for each command,
  then always saves the file back to the block afterwards, "as rclone may
  have refreshed the token, even if the command itself failed"
  (`upload_asset.py`, `RCloneConfigSavedInPrefect.update_config`).
- An existing production pipeline uses exactly this, with a `type = onedrive`,
  `drive_type = documentLibrary` remote.

hp supports the same pattern:

- **URL.** `rclone://<remote>/<path>` names a remote and a path.
- **Config source** in `storage_options`, exactly one of:
  - `config_block: <name>`, an `RCloneConfigFileBlock` document. Existing
    blocks are used as they are. hp registers an identical block type (same
    name, slug and fields) when prefect-managedfiletransfer is not installed,
    and does not depend on that package.
  - `config_file: <path>`, for single-host setups only. hp warns when it is
    used with an external Prefect.
- **Several rclone locations** may use different blocks. At session start
  hp concatenates their sections into one temp config; duplicate remote
  names are a config error.
- **Write-back.**
  - On session end, each block whose sections changed is saved back, even
    after a failure.
  - The temp config lives in a private 0700 directory and is created 0600.
  - It is deleted afterwards.
- **Security note for the docs:** `config_file_contents` is a plain string
  field and is visible in the Prefect UI. Anyone with Prefect UI access can
  read the tokens.

### One rcd per rclone config across the cluster, started locally (STO-20)

What has to be single is **the user of each rclone config**, not rclone
itself. Two rcd processes holding the same config can both refresh one OAuth
token. With Box's single-use refresh tokens, one of them then locks the
account out. Both also save the whole config back to the block, and the last
save wins. Configs that share nothing cannot interfere. So the lock is per
config source:

- the lock is `rclone:<block name>` (or `rclone:file:<path>`), limit 1;
- in the usual setup each project has its own block, so this means one rcd
  per project;
- projects that share a block are serialised together, which is what keeps
  them safe;
- different projects' rcds run in parallel, even on the same node, each on
  its own socket;
- locking per project instead would be unsafe when two projects share a
  block. Locking per remote would be unsafe because write-back replaces the
  whole block.

A transfer between two rclone locations with different blocks takes both
locks in one `hp_concurrency([...])` call (all or nothing, so no deadlock)
and starts one rcd with both configs.

All rclone work goes through `rclone_session(config_sources)` in
`storage/rclone/session.py`:

1. **Take the lock.** Acquire one slot of each `rclone:<source>` Prefect
   global concurrency limit (limit 1). The limits are created at startup with
   `ensure_concurrency_limit` and taken through `hp_concurrency` in
   `server/services/concurrency.py`. Prefect's slot leases expire if the
   holder dies, so a crashed node frees the lock.
2. **Start rcd on this node.** Load the config blocks and write the temp
   config, then start `rclone rcd --config <tmp> --rc-serve` with a random
   32-byte `--rc-user`/`--rc-pass`. It listens on a Unix socket in the
   private directory when rclone supports that (confirmed in the spike),
   otherwise on `127.0.0.1:<free port>`.
3. **Do the work.** Yield the fsspec filesystem bound to this rcd.
4. **Always clean up.** Stop rcd, save changed config back to the blocks,
   remove the temp directory and release the lock.

Rules for using the session:

- **Re-entrant in a process.** Nested use reuses the open session, using a
  process-local reference count.
- **How long the lock is held.** Flow tasks hold the session for one batch of files
  (the rule's or index's existing batch), not for a whole run, so runs on
  other nodes interleave between batches. API requests wait at most
  `rclone.api_wait` (default `30s`), then return `503` with `Retry-After`.
- **Multi-node behaviour.** Whatever node a Prefect run lands on (several
  `server start --no-api --no-ui` replicas on Docker Swarm), it starts its
  own rcd only while holding the lock, and talks to it only locally. No
  network path to rcd exists, and nothing has to be shared between nodes
  except Prefect (lock and blocks).
- **rclone as the first storage location: not in M7 (recommended).**
  - The first location takes every API upload, overwrite, download and
    preview. On rclone each of those would need the config lock, so users
    would queue behind rule and index batches, with `503` under load.
  - Each request would also pay rcd start-up (about 1s) and the provider's
    throttling.
  - On SharePoint, every uploaded Office file would fail verify, and
    staged `.hp-tmp` overwrites would show up in the library's version
    history.
  - The usual need, files dropped into SharePoint or Box, is already covered.
    An rclone location can be indexed for external changes, and a rule can
    copy those files into a local or S3 first location.
  - The restriction is only a config check, so it can be lifted later
    without redesign. The case is recorded in `requirements/later.md`.
- **Constraints for rclone locations:**
  - an rclone location cannot be a project's first storage location (the
    upload target); this is a config error;
  - clients never get direct access: the API marks rclone locations
    `direct_access: false`, and the client transfers through the API;
  - throughput is serialised per rclone config, cluster-wide, which the docs state plainly.

### Securing rcd (STO-22)

- **Never exposed beyond the node.** rcd is never started with
  `--rc-no-auth`, `--rc-web-gui` or `--rc-enable-metrics`, and never on a
  non-loopback address. Its credentials are random per session and held only
  in hp's memory.
- **Only allowlisted endpoints.** hp's rc client is a fixed allowlist:
  `operations/{list,stat,about,hashsum,deletefile,movefile,copyfile,mkdir,rmdir}`,
  `config/listremotes`, `core/version` and `--rc-serve` GETs. It never calls
  `config/*` writes or `core/command`.
- **Bound to hp's lifetime.** The rcd child is killed with hp: it runs in its
  own process group, with a parent-death signal on Linux and an atexit
  handler.
- **Redaction.** rcd credentials and the config contents are redacted in every
  log line and error.

### Filesystem behaviour (STO-05)

- **Reads** stream with HTTP Range from `--rc-serve`.
- **Writes** spool to a local temp file while hp computes MD5. Commit is
  `operations/copyfile` from the local spool path, which works because rcd
  is on the same node. The size is known, so OneDrive and SharePoint (no
  streamed upload) use rclone's normal chunked upload with retries. A
  failing source never reaches the remote; abort only deletes the spool.
- **Moves.** `move_replaces` is true: rclone deletes an existing destination
  before moving.
- **Free space** comes from `about` when it reports `free`, otherwise None.
  The `quota` setting covers the rest. rclone's docs disagree on whether
  `about` works on Box.
- **Code location.** Code sits in `src/heliopuddle/storage/rclone/`,
  registered through an `fsspec.specs` entry point. The STO-06 check
  exempts it as a backend package, and `backends.py` gains `rclone` entries.
  It uses httpx, which is already a dependency.

### Verification hash (decided: native hash with fallback)

Box reports SHA1 and OneDrive and SharePoint report QuickXorHash; neither
reports MD5. Each rclone location has `verify: auto | native | content | size`,
default `auto`. The test backend is also decided: a WebDAV container, plus a
WireMock token endpoint for write-back.

- **native.** After upload, compare the provider's server-computed hash
  (`operations/list` with `showHash`) with the same hash of the local spool,
  computed by the local rcd (`operations/hashsum` on the spool path). This
  reads metadata only, with no download. QuickXor is not cryptographic but
  catches corruption. hp still records MD5, computed while spooling, as
  `file.hash`, and stores the native hash on the copy for cheap later
  re-checks by index and reconcile.
- **content.** Download the destination and compare MD5, as hp does today.
  This is the strongest check (it catches SharePoint changing Office files)
  but doubles traffic and uses up provider rate limits.
- **size.** Size only, which is what prefect-managedfiletransfer relies on
  ("rclone is smart enough").
- **auto.** native when the provider has a hash, otherwise content.

This amends STO-03 and the design rule "verify always reads content" for
rclone locations: a provider-computed hash of the stored bytes counts as
verifying the destination hash.

### rclone binary (DEP-21)

- The Docker image bundles a pinned rclone, with its checksum verified.
- Elsewhere, hp looks for `HELIOPUDDLE_RCLONE_BINARY`, then `rclone` on PATH.
  If neither is found and an rclone location is used, hp **downloads the
  pinned version** to `<data dir>/bin/rclone-v<ver>/`:
  - the source is
    `https://downloads.rclone.org/v<ver>/rclone-v<ver>-<os>-<arch>.zip`;
  - the SHA256 is checked against hashes pinned in hp's source for that
    version (rclone's `SHA256SUMS` are PGP-signed; pinning avoids needing
    gpg);
  - `HELIOPUDDLE_RCLONE_AUTO_DOWNLOAD` (default true) turns this off;
  - if the download fails, the location is marked incomplete with the
    reason. It does not stop startup.

### Why we are confident about Box and SharePoint

- **Proven in production.** An existing production pipeline already uploads to SharePoint
  through rclone (`type = onedrive`, `drive_type = documentLibrary`), with
  the same config-in-block and write-back pattern hp adopts.
- **Tested by rclone.** rclone's own nightly integration matrix
  (`fstest/test_all/config.yaml`) runs `TestBox:`, `TestOneDrive:` and
  `TestOneDriveBusiness:`.
- **hp uses only basic operations.** hp calls only rclone's most basic
  operations (list, stat, copyfile from a local file, movefile, deletefile,
  mkdir, hashsum, about), all of which rclone's overview marks as supported
  on Box and OneDrive. Copyfile from a local file has a known size, so
  OneDrive's lack of streamed upload does not matter.
- **The provider-specific gaps are designed in.** No MD5 is handled by native
  verify. Uncertain `about` falls back to None and quota. Case-insensitivity
  and SharePoint's changes to Office files are documented. Token refresh is
  handled by write-back under the per-config lock.
- **What tests cannot cover.** Tests do not reach real Box or SharePoint. The
  owner can run the conformance suite against real remotes before release, by
  pointing the WebDAV test's remote at a real `config_block`.

### Box and SharePoint notes (for the docs)

- **SharePoint and OneDrive.** Use `type = onedrive` with
  `drive_type = documentLibrary`. SharePoint "silently modifies uploaded
  files, mainly Office files" (rclone onedrive docs). Under `content` or
  `native` verify this is caught and the source is kept. Data files are not
  affected.
- **Box tokens.** A Box OAuth "Refresh Token is valid for 60 days and can be
  used … only once"
  ([Box: refresh tokens](https://developer.box.com/guides/authentication/tokens/refresh/)).
  hp's single rcd plus write-back keeps that safe.
- **Box JWT.** With JWT (`box_config_file`, `box_sub_type = enterprise`), a
  token is obtained by exchanging a newly signed assertion
  ([Box: JWT](https://developer.box.com/guides/authentication/jwt/),
  [without SDK](https://developer.box.com/guides/authentication/jwt/without-sdk/)).
  Those pages never mention a refresh token. That JWT therefore avoids the
  single-use problem is an inference from Box's docs, not a statement in
  them, and the docs will say it that way.
- **Box limits.** Names are case-insensitive and at most 255 characters
  (rclone box docs).

## Requirements in scope (for the spec review)

| REQ | Description |
|---|---|
| STO-05 (rewritten) | **rclone storage locations.** `rclone://<remote>/<path>` is an fsspec backend over a local rcd. It passes the STO-07 conformance suite through rclone's webdav backend against a WebDAV container. Writes spool locally and commit with `operations/copyfile`; a failing source never reaches the remote. Reads stream. `move_replaces` is true. `verify` is `auto`/`native`/`content`/`size`. |
| STO-17 | **SFTP through fsspec only.** Conformance tests run against a real SFTP container, covering replacing moves (`posix-rename`), `statvfs` free space, root creation, and key and password auth. |
| STO-18 | **Credentials** come inline, from `${VAR}` or native environment variables, or from `{{ prefect.blocks.secret.<name> }}`, with whole-value JSON allowed. A missing block never stops startup: the location is `incomplete`, its runs and calls fail with a clear message, and the API (`status`, `problems`) and the UI show a warning. Blocks resolve lazily with a 60s cache, so no restart is needed after creating one. Values are redacted. |
| STO-19 | **RemoteFileSystem block.** `block: <name>` supplies `url` and `storage_options`, with inline options merged over the block's. `block` together with `url` is a config error. |
| STO-20 | **One rcd per rclone config, cluster-wide.** Every rclone interaction holds the Prefect global concurrency limit `rclone:<config block>` (limit 1) for each config it uses; different configs (normally different projects) run in parallel. rcd is started locally under the lock and stopped before release. The rclone config comes from `RCloneConfigFileBlock` documents (compatible with prefect-managedfiletransfer) or a file, and is saved back to its blocks after every session, including failed ones. API waits are bounded (`503` with `Retry-After`). Tested with two hp service processes, as on two nodes: rcd lifetimes never overlap. |
| STO-21 | **Token write-back with a real rclone.** A onedrive remote with an expired token and `token_url` pointed at WireMock is refreshed by rclone, and the new token lands in the block. The API call afterwards fails, which is expected, and still triggers write-back. |
| STO-22 | **rcd is private.** It listens on a Unix socket or loopback only, always with auth and per-session random credentials. It runs with no GUI or metrics, and hp calls only an allowlist of rc endpoints. rcd is killed with hp. rclone locations are never directly accessed by clients and cannot be a project's first storage location. Credentials and config contents are redacted. |
| DEP-21 | **rclone binary.** It is bundled in the Docker image (checked by e2e). Elsewhere it is auto-downloaded at the pinned version with pinned SHA256 hashes, which can be turned off. A failure marks the location incomplete. |
| DEP-22 | **User docs.** `docs/site/storage.md` covers each backend, the credential routes, the rclone block pattern, the single-rcd model and its throughput limit, Box and SharePoint notes, and verify modes. The environment variable table is regenerated. |
| DEP-12 (amended) | `heliopuddle[sftp]` is described as fsspec's SFTP dependency. |

Removed: `mirror` rules, "SFTP (paramiko)", and the rcd sidecar/service.

## Work, in milestone order

1. **Docs first (one commit).**
   - `docs/spec/features.md`: rename M7, rewrite its section, update the
     status table, and add a "M7 review decisions" table.
   - `docs/spec/requirements/storage.md` and `deploy.md`: the requirements in
     the table above.
   - `docs/design/storage.md`: credentials, the rclone session and lock,
     verify modes, and the backend table.
   - `docs/design/architecture.md`: the `rclone:<config>` concurrency limits.
   - `docs/spec/reviews/m7-plan.md`: this plan.
   - README and CLAUDE.md wording.
2. **Spike** (hours, not days), with the results recorded in
   `docs/design/storage.md`:
   - rcd on a Unix socket, reached with httpx's UDS transport;
   - that `operations/copyfile` from a local path and `hashsum` on a local
     path work;
   - that Prefect concurrency leases are released after `kill -9` of the
     holder.
3. **Build**, test-first and tagged by REQ, delegated by area:
   - **SFTP:** STO-17.
   - **Credentials:** STO-18 and STO-19.
     - Code: `config/loader.py` (syntax check), `storage/credentials.py`,
       `server/schemas/projects.py` (status and problems), the 409 error and
       the UI banners.
   - **rclone filesystem:** STO-05.
     - Code: `storage/rclone/{fs,client,session,binary}.py` and
       `backends.py` entries.
     - Unit tests run against a fake rc server.
   - **Session and lock:** STO-20 and STO-22.
     - Tested with the Prefect test harness and two processes.
   - **Write-back:** STO-21, using WireMock as in `tests/integration`.
   - **Binary:** DEP-21, covering the Dockerfile and the auto-download.
     - Unit tests use a local HTTP server serving a fake zip with a pinned
       hash.
   - **Docs site:** DEP-22.
4. **Verify**, then open a PR to `main`.

## Verification

- **Suites.** `uv run pytest tests/unit -n auto --dist loadfile` and
  `uv run pytest tests/integration` (SFTP container, WebDAV container
  through rcd, WireMock token endpoint, Prefect blocks and limits, Azurite).
  Coverage ≥ 90%, and `scripts/req_matrix.py` complete.
- **Lint gates:** ruff, pyright, `lint-imports`, `check_file_length.py`,
  `pylint src` and `mkdocs build --strict`.
- **E2E:** image build, `rclone version` in the image, and the Playwright run.
- **Manual:**
  - start `server start` with no rclone on PATH and confirm the
    auto-download;
  - start with an `rclone://` location whose `config_block` does not exist
    yet: the UI shows the warning;
  - create the block in the Prefect UI: the location becomes ok without a
    restart;
  - run a copy rule to rclone and back;
  - run two `--no-api --no-ui` processes against one external Prefect and
    check in the logs that rcd sessions for one block never overlap, while two projects with separate blocks do run together.

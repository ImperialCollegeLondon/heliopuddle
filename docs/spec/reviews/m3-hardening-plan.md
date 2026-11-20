# M3 hardening plan (approved): operation ownership, safe publication, failure semantics

## Context

An independent review of M1–M3 (PR #3, unmerged) reproduced data-integrity defects that the 673/64/7 green test suites did not catch. The core gap is **operation ownership**: an upload, verify or delete spans several phases (check, storage I/O, register), but only the individual DB mutations are locked. So:

- concurrent uploads can clobber a registered file;
- uploads share one temp path;
- a failed object-store upload can publish partial bytes;
- stale verifies and deletes can act on newer content;
- a changed storage URL silently re-points existing copies;
- deployment names can collide.

The review also found correctness, scale and documentation issues.

You decided:

- this work stays **on PR #3**, so M3 is accepted only with these guarantees;
- the `temp_writes` setting is **removed**. New files are written straight to the final path while hp holds a reservation on it. Overwrites are staged to a unique `.hp-tmp` path, then published.

Guiding rule: one small, explicit lifecycle contract with expected revisions, rather than more preflight checks. Prefect stays the only background-work system.

## Design

### O1. Path ownership: copy states plus a reservation token (fixes P1.1, P1.3)

Ownership reuses the copy **state**; there is no separate `op_kind`. Migration `0004` adds these columns to `file_storage_location`:

| Column | Meaning |
|---|---|
| `generation` | Integer, incremented on every content-changing transition |
| `op_token` | UUID of the operation that owns the copy |
| `op_deadline` | When the reservation expires |
| `pending_hash` / `pending_size` | The content an overwrite intends to write |

Each in-flight state has exactly one meaning:

| State | With a live `op_token` | Without a token |
|---|---|---|
| `pending` | a new-file upload is reserved or being written; not latest-eligible | content known (it has a hash), awaiting verification; latest-eligible as today |
| `overwriting` (**new**) | the old content stays valid and served, latest-eligible; the new content is staged, not yet published | (not used) |
| `deleting` | a delete is in progress | a delete that failed earlier; repaired by the index |

A copy with a live, unexpired `op_token` is **busy**. Every other operation on that path and storage location gets `409 busy` (Retry-After), and the index skips it.

The client protocol replaces "check → write → register":

1. **Reserve.** `POST /projects/{p}/uploads {path, hash, size, storage_location?, overwrite?, metadata?, content_date?}`. Under the series lock the server decides:
   - **unchanged:** same hash and the copy is available. Nothing to write; the existing record is returned.
   - **conflict:** different hash, no `overwrite`. Returns `409` with the existing record.
   - **busy:** another operation holds the path. Returns `409 busy`.
   - **new path:** creates the file row and a copy in state `pending` with a token. `write_path` = the final path.
   - **overwrite:** the available copy moves to `overwriting` with a token and `pending_hash`/`pending_size`; it keeps serving the old content. `write_path` is a unique staging path `<path>.<token>.hp-tmp`.

   The response is `{token, mode, write_path, deadline, file?}`.
2. **Write** to `write_path` (the client streams to the store).
3. **Complete.** `POST /uploads/{token}/complete`. The server checks the token is still current (409 if it was taken over) and runs `verify_file` with the expected generation. Then:
   - **new path:** the copy becomes `available`, or `unknown` on a mismatch;
   - **overwrite:** the publish protocol O2 runs, and the copy ends `available` with the new hash, or back at `available` with the old hash on failure.
4. `POST /uploads/{token}/renew` extends the deadline during long transfers; the client renews automatically. `DELETE /uploads/{token}` aborts.

Other ways in:

- The existing `POST /projects/{p}/files` registers files written by other tools. It reserves and completes atomically, for a new path or the same hash only.
- An expired reservation is cleaned up by the next full index, or by whoever reserves next:
  - **new-path `pending`:** the partial final object is deleted only if it does not match the reserved hash, and the reservation is dropped;
  - **`overwriting`:** the staged file is deleted and the copy goes back to `available` with the original;
  - **`deleting`:** the delete is retried.

### O2. Overwrite publication (fixes P1.1 overwrite, P2 non-replacing mv)

The new content is fully staged and verified before the original is touched. The goal is that at every crash point either the original or the complete new file is at the final path.

1. Verify that the staged `.hp-tmp` hash equals `pending_hash`.
2. Publish, using a capability from `storage/backends.py`, `move_replaces(protocol)`:
   - **Replacing backends** (local, memory, object stores; on object stores a move is copy then delete): `fs.mv(staged, final)`.
   - **Non-replacing backends** (SMB, SFTP rename):
     1. `fs.mv(final, <path>.<token>.hp-old.hp-tmp)`, keeping the original as a backup;
     2. `fs.mv(staged, final)`;
     3. if step 2 fails, move the backup back.
3. Re-verify the final file's hash. On a mismatch, restore the backup where one exists. Then, under the series lock with the expected generation: set the new hash and size, `generation+1`, clear the token, and recompute latest.
4. Delete the backup.

An orphaned `*.hp-old.hp-tmp` or staged file belonging to an expired token is repaired by the full index: if the final file is missing or wrong, the backup is restored; otherwise it is deleted.

### O3. Unique, aborting writes (fixes P1.2, P1.3)

- **Unique staging paths.** Every staged write uses `<path>.<token-or-uuid>.hp-tmp`. Only that attempt's file is ever cleaned up. The index already excludes the `.hp-tmp` suffix.
- **No commit on a failed copy.** `StorageLocation.write` opens with `autocommit=False` where the file object supports it. On a source error it calls `discard()` and never `close()`, so nothing is committed. Otherwise it falls back to a best-effort abort helper in `backends.py`.
- **The protocol is the real guarantee:** an aborted new-path upload never completes, so it is cleaned up per O1, and an aborted overwrite never touches the final path.

### O4. Guarded transitions (fixes P1.4, delete races)

One small helper module, `server/models/transitions.py`:

- `expect(copy, generation, states, not_deleted=True)` runs under the series lock and raises `StaleOperation` if anything changed.
- **Verify** carries `(copy_id, generation)`. A result is applied only if the generation, the file's live state and the hash are unchanged; otherwise it is dropped and logged.
- **Delete:**
  1. moves the copy to `deleting` with a token, which stops new uploads to that path until it finishes;
  2. deletes the store object only if its current `info()` matches the copy's last observation (size and mtime, plus native md5 when present). If not, it refuses: the copy becomes `unknown` and the store is left untouched;
  3. finishes with the expected generation.
- **Index and accept** use the same helper.

### O5. Storage URL changes (fixes P1.5)

Treat this as "the user moved the disk and the data with it". The same files are expected at the new URL.

During config sync, if a storage location's URL changes:

1. Log a loud WARNING naming the old and new URL and saying a re-check is running.
2. Keep every copy's **hash** and size. Set state `pending` (known content awaiting verification, still latest-eligible) and clear the observations (`mtime`, `last_seen_at`). Bump generations and clear any tokens. Reset `index_progress`. Store the new URL.
3. Check the root at startup; it **fails hard** if unusable, per `create_root`.
4. Adoption runs an `index_full` (because `last_full_at` is now null). For each listed file with a `pending` copy, the index re-hashes it and compares with the kept hash:
   - match → `available`;
   - mismatch → `unknown`.
5. Copies not found are handled as normal: `deleted` (accepting storage location) or `missing`. New files are added as normal.

A delete never removes an object hp has not observed in the current root (O4 step 2), so a copy that hasn't been re-checked yet can't cause a wrong delete.

### O6. Deployment identity (fixes P1.6)

- **Collision-free names:** `index-full-p{project_id}-sl{storage_location_id}-{project}-{sl}`. The ids make it unique; the names keep it readable.
- **Duplicates rejected:** `build_deployments` fails if two specs share an identity.
- **Explicit parameters:** runs submitted by the API and by adoption pass `project` and `storage_location` explicitly.

### Smaller fixes

| # | Fix |
|---|---|
| F1 | **Incremental watermark.** The saved watermark is `listing_start − settle_time`, so files skipped while settling stay eligible next run. Config validation also warns when `incremental_overlap < settle_time` is irrelevant. |
| F2 | **Failure semantics.**<br>• Expected incomplete delete: `200` with state `deleting`.<br>• Unexpected exception: `500` with the error.<br>• Repair results are inspected: an incomplete repair counts as `repair_failed`, the run ends failed, and progress is not advanced. |
| F3 | **Consistent observations.** Verify checks the registered size, the stored size and the hash together; any mismatch → `unknown`. The indexer stats, hashes, then stats again; if anything changed it skips the file until next run. Size, mtime and hash always come from one stable observation. |
| F4 | **Cross-project ids.** Client `open`, `download` and `accept` by id reject a record from another project (`NotFoundError`) before touching storage. |
| F5 | **Split-port shutdown.** Wait only on unfinished server tasks. One signal requests a graceful stop once; only a second signal forces it. Lifespans run on shutdown. |
| F6 | **Scale.**<br>• Listing streams with `fs.walk(detail=True)`, with no whole-tree list and no global sort.<br>• Removal detection is set-free: listed copies get `last_seen_at = listing_start`, and removals are copies of that storage location with an older `last_seen_at`.<br>• `recompute_latest` selects the top eligible version in SQL (`ORDER BY … LIMIT 1`) instead of loading every member.<br>• New scale tests: 20k files in one tree, a 2k-version series, a bounded time, and no O(n²). |
| F7 | **Concurrent first start.** Concurrency-limit creation handles conflicts by re-reading. Adoption submits with a Prefect `idempotency_key` = `adopt-sl{id}-g{storage_location_generation}`, so racing starts create one run. |
| F8 | **Series key namespace.** Generated keys are prefixed `v:`, literal unversioned paths `p:`. Migration `0004` rewrites existing keys. |
| F9 | **Config validation.**<br>• Prefix templates are parsed at load time (format syntax plus a strftime test), so an unmatched brace fails at startup.<br>• A version group that matches a non-integer gives `422` with the rule index and the value; the indexer marks such a file `unknown` with a logged reason. It never crashes. |
| F10 | **Remove `temp_writes`.** It is removed from config, the API, the client and the docs, and from STO-01/STO-07. |

### Documentation consolidation

Rewrite rather than append:

- `data-model.md`: one current description of copy states (`pending` with or without a token, the new `overwriting`, `deleting`), latest eligibility (`available`, `overwriting`, token-less `pending`), `generation` and the reservation columns.
- `versioning.md`: remove the promise of re-derivation on rule changes.
- `operations.md`: rewrite "Add a file", overwrite, delete and accept around O1–O4. Remove the native-md5 claim for accept.
- `storage.md`: writes and staging per O1–O3; `move_replaces`; drop `temp_writes`.
- `indexing.md`: the watermark, the observation rule, scale.
- `deployment.md`: the Docker quick start explains that `file:///data` is the container's path. Host-side clients need the same absolute path mounted (`-v /srv/hp:/srv/hp`) or must run in a container sharing it.
- README: check that every documented step runs as written.
- The approved follow-up plans in `docs/spec/reviews/` stay as history. `features.md`'s M3 section and the requirements become the single current contract.
- `operations.md` rewritten as the ownership contract (O1–O4), the reference for every future operation (copy and move in M4 will use it). (Owner decision: no separate lifecycle doc.)

## Requirements (all due in M3)

| ID | Requirement | Verified by |
|---|---|---|
| **STO-09** | **Path ownership.** One operation at a time owns a (project, path, storage location): upload reservation, overwrite or delete, each with a token and a deadline. Others get `409 busy`, and the index skips owned paths. New files are written straight to the final path by the reservation holder only. Renew, abort and complete endpoints exist. Expired reservations are cleaned up without touching content that matches the reserved hash. | Deterministic phase-interleaving tests:<br>• two clients on an absent path → exactly one owns it, and the winner's bytes and record survive intact;<br>• expiry and takeover;<br>• crash before complete. |
| **STO-10** | **Overwrite publication.** At every step, either the complete original or the complete verified new content is at the final path. Replacing and non-replacing move semantics come from `backends.move_replaces`. Orphaned staged and backup files are repaired by the full index. | Fault injection at each publish step on a replacing fake and a non-replacing fake filesystem (memory subclasses), plus Azurite. Invariant: the final file's hash ∈ {old, new}, and the DB hash equals the store hash. |
| **STO-11** | **Unique staging paths.** Every staged write uses a unique `<path>.<id>.hp-tmp`, and only its owner cleans it up. | Concurrent identical uploads both behave correctly; no cross-cleanup. |
| **STO-12** | **No partial publication.** A source failure mid-write never commits partial bytes over an existing object. New-path partials are never `available` and are cleaned up. | A source that yields some bytes then raises: a real s3fs writer with network calls intercepted, plus memory/local; the existing final file is unchanged. |
| **STO-13** | **Safe delete.** A delete removes a store object only if it matches hp's last observation of that copy, and it is guarded by generation. A stale delete never removes newer content. | Interleaving tests (delete versus re-upload); a URL-change scenario. |
| **SVC-11** | **Guarded verify.** A verify result is applied only if the copy generation, live state and hash are unchanged since the verify started. | Interleaving tests (verify against overwrite, verify against delete). |
| **SVC-12** | **Storage URL change.** A loud warning. Copies keep their hash and size but become `pending` with observations cleared; index progress is reset; startup fails if the new root is unusable. Adoption re-checks each copy: same content → `available`, different → `unknown`, absent → `deleted`/`missing` as normal. | Unit and integration tests. |
| **SVC-13** | **Deployment identity.** Deployment names include the project and storage-location ids. Duplicate identities are rejected before registration, and submissions pass explicit parameters. | Unit test with `alpha-beta/primary` and `alpha/beta-primary`; integration test. |
| **SVC-14** | **Incremental watermark.** It never permanently skips a settling file (F1). | Unit test with `settle_time` > overlap. |
| **SVC-15** | **Failure semantics.** As F2. | Unit tests: an unexpected delete error → 500; an incomplete repair → the run fails and progress is not advanced. |
| **SVC-16** | **Consistent observations.** As F3. | Unit tests: a size-mismatch register → `unknown`; a file changing during hashing → skipped. |
| **SVC-17** | **Scale.** As F6. | Scale tests with time and memory bounds (marked `slow`, run in the unit suite with modest sizes). |
| **SVC-18** | **Concurrent first start.** It is idempotent (F7). | Unit tests with racing probes; integration test with two processes. |
| **API-13** | **Series key namespace.** Literal paths and generated keys never collide (F8); migration `0004`. | Unit test (`x_v1.dat` vs `x_v{v}.dat`); migration test. |
| **CLI-08** | **Cross-project ids.** The client rejects records from another project before touching storage (F4). | Unit test. |
| **DEP-11** | **Split-port shutdown.** Clean with one signal, forced only with a second; no busy loop (F5). | Unit test with two real uvicorn servers. |
| **DEP-12** | **Config validation.** As F9. | Unit tests. |
| **STO-01 / STO-07** (amended) | `temp_writes` is removed. Conformance adds unique staging, abort-on-failure and both move semantics. | Conformance suite. |

Every new test asserts **invariants over both storage and DB state**, not just the exception. Existing race tests are strengthened: for example, the client race test also checks that the winner's bytes survived.

## Build

1. **Docs commit by me:** the rewritten `operations.md`, the consolidated design docs, the requirements, `features.md` decisions (H1 = this plan), and `docs/spec/reviews/m3-hardening-plan.md`.
2. **Scaffold by me:** migration `0004` (copy columns, `overwriting` state, series-key rewrite), the ORM and the schemas (`UploadReservation`, `UploadMode`).
3. **Agents in parallel worktrees, test-first, each owning REQ ids:**
   - **A, lifecycle core:** O1, O2, O4, the transitions module, the uploads API, delete, verify, accept. Owns STO-09/10/13, SVC-11, API-13 and SVC-15 (delete).
   - **B, storage and client:** O3, `backends.move_replaces`, the fakes and conformance, the client's reserve, write, complete and renew flow, F4, F10. Owns STO-11/12, CLI-08 and STO-01/07.
   - **C, indexing and runtime:**
     - indexer changes: O5, O6, F1, F3 (index side), F6, F7 and the index repair of orphans and expired reservations;
     - runtime and config: F5, F9;
     - owns SVC-12/13/14/16/17/18, DEP-11 and DEP-12, plus SVC-15 (repair).
4. **Integrate.** Then an **adversarial review pass**: a fresh agent given only the code and the invariants (not my conclusions) tries to break the lifecycle with interleavings and fault injection. Anything it reproduces is fixed before the PR update.
5. **Verify**, then push to PR #3.

## Verification

- ruff, pyright, `lint-imports`, the STO-06 guard and `check_deps`.
- Unit tests at ≥90% coverage, then integration (Postgres, external Prefect, Azurite), then Docker e2e including the browser UI tests.
- `req_matrix.py` for M3 is OK.
- A manual run:
  - two concurrent clients uploading to the same new path: one wins, the other gets `busy`, the bytes are intact;
  - an overwrite with a kill between staging and publish: the original is intact and the index repairs it;
  - a storage URL change: a warning, then a reset, then re-adoption;
  - zero-config start, a client add, the UI, Prefect runs.
- Push to PR #3 with a summary comment mapping each review finding to its fix and test.

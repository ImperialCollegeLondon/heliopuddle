# M3 follow-up plan (approved): generic fsspec storage, dependency audit, hashing

## Context

You reviewed M3 (PR #3, not yet merged) and asked for three changes before merging:

1. **Storage must be generic over any fsspec backend.** The code has Azure-specific logic: protocol lists in `storage/location.py`, Azure container creation, and Azure/S3 metadata parsing for md5. You want any backend-specific code removed, or at most minimised and isolated. The out-of-the-box targets are **local, SMB, SFTP, S3, Azure and GCS**, all through their fsspec implementations. Tests may keep Azure (Azurite) as the canonical cloud backend for the few slow integration tests, alongside local and memory.
2. **Dependency audit, and a rule for the future.** I checked every dependency against PyPI today:
   - All are on the latest release except FastAPI (0.150.0 vs 0.162.0), which Prefect 3.9 caps.
   - adlfs is locked at 2026.8.0, the latest release. But `pyproject.toml` declares `adlfs>=2024.12`, and several other lower bounds are similarly stale, which is what looked old.
   - The SMB, SFTP, S3 and GCS backend packages are not installed at all.
3. **Hashing:** you chose to keep md5 everywhere for now.

This work goes on the M3 branch and updates PR #3. It is not a new milestone.

## Decisions

| # | Topic | Decision |
|---|---|---|
| G1 | Generic by default | Storage code only uses the generic fsspec API: `open`, `info`, `find`, `exists`, `rm`, `mv`, `makedirs`. **No protocol names anywhere in `src/` except one isolated module, `storage/backends.py`.** A unit test enforces this by scanning the source tree for protocol names and backend packages outside that module. |
| G2 | Temp-then-rename writes | Each storage location has a new setting, `temp_writes: true \| false \| auto` (default `auto`).<br>• `true`: write `<path>.hp-tmp`, then `fs.mv` into place. This works on every backend.<br>• `false`: write the final path directly.<br>• `auto`: `backends.py` holds the one table of object-store protocols (`s3`, `s3a`, `gs`, `gcs`, `az`, `abfs`, `abfss`, `adl`, `oss`, `r2`). Those get `false`, because their uploads only become visible when complete and a rename would be a server-side copy. Everything else gets `true`.<br>• This replaces the current `_RENAME_PROTOCOLS` list. |
| G3 | Buckets, containers and shares | hp never creates the storage root's bucket, container or share. Sub-directories are created generically with `fs.makedirs(parent, exist_ok=True)`, a no-op on object stores. If a write fails because the root is missing, hp raises a clear "the storage location root (bucket/container/share) must exist" error. The Azure container auto-creation code is removed, and the tests and the Docker e2e fixture create the container first. |
| G4 | Native md5 (index only) | The backend-reported md5 is still used only to skip reading objects during indexing. The metadata parsing moves into `backends.py` as one function, `native_md5(info)`. It checks the generic `info` keys fsspec backends expose (`md5`, `content_md5` / `content_settings.content_md5`, GCS `md5Hash`, and an S3 single-part `ETag`). **Verify and accept always read the content.** Today accept trusts the native md5; that is fixed. |
| G5 | Hash | md5 stays, as you chose. The docs say plainly that it is a comparison hash computed by hp in Python, and that native md5 is only an indexing shortcut. |
| G6 | Backends out of the box | New optional extras in `pyproject.toml`:<br>• `heliopuddle[s3]`: `s3fs`<br>• `heliopuddle[azure]`: `adlfs`<br>• `heliopuddle[gcs]`: `gcsfs`<br>• `heliopuddle[sftp]`: `paramiko`, for fsspec's built-in `sftp://`<br>• `heliopuddle[smb]`: `smbprotocol`, for fsspec's built-in `smb://`<br>• `heliopuddle[all]`<br>The Docker image and the dev group install `all`. adlfs moves out of the core dependencies. |
| G7 | Conformance suite | One storage conformance test module runs the same tests against every backend fixture: memory and local in `tests/unit`, Azurite in `tests/integration`. A new backend gets tested by adding a fixture. A unit smoke test checks that each of the six target protocols resolves to an importable fsspec class and that `StorageLocation` builds for its URL form. |
| G8 | Dependency policy | 1. Lower bounds in `pyproject.toml` are set to the current latest releases.<br>2. New `scripts/check_deps.py` prints locked vs latest from PyPI and the reason a dependency is held back (e.g. FastAPI capped by Prefect).<br>3. At the start of every milestone: run `uv lock --upgrade`, run the script, and fix or explain anything behind.<br>Both the policy and the storage rules go into CLAUDE.md as standing rules. |

## Requirements added or changed (all due in M3)

| ID | Requirement (detailed) | Verified by |
|---|---|---|
| **STO-06** (new) | **Storage is generic over fsspec.**<br>• `StorageLocation`, the client, the services and the indexer use only the generic fsspec filesystem API.<br>• Backend-specific knowledge (the object-store table for `temp_writes: auto`, and native md5 extraction) lives only in `heliopuddle/storage/backends.py`.<br>• Supported out of the box, through fsspec and the `heliopuddle[all]` extras: local (`file://`), SMB (`smb://`), SFTP (`sftp://`), S3 (`s3://`), Azure Blob (`az://`, `abfs://`) and GCS (`gs://`, `gcs://`).<br>• Any other fsspec backend works once its package is installed. | A unit test fails if a protocol name or backend package is referenced in `src/` outside `storage/backends.py`; a smoke test for all six target protocols. |
| **STO-07** (new) | **Conformance suite.** The same suite runs against every test backend: write (with and without temp writes), read, md5, `info`, `exists`, `rm`, recursive `list_files`, a missing prefix, a missing root (clear error, no auto-create) and paths outside the root rejected. | Memory and local in unit tests; Azurite in integration tests. |
| **STO-02** (reworded) | Azure Blob is the canonical cloud backend for the slow integration tests: STO-07 runs against Azurite, plus indexing on Azurite, plus the Docker e2e test (DEP-03). | Integration and e2e tests. |
| **STO-01** (amended) | `temp_writes` (G2): `true`, `false` or `auto`, set per storage location. | Unit tests. |
| **SVC-03** (amended) | Native md5 is an indexing shortcut only (G4). Verify and accept always read the content. | A unit test where a forged native md5 leaves the copy `unknown` under verify and accept. |
| **DEP-08** (new) | The `heliopuddle[s3,azure,gcs,sftp,smb,all]` extras exist. The Docker image installs `all`, and every target protocol resolves inside the image. | A unit test for the extras and protocols; an e2e check that runs Python in the image. |
| **DEP-09** (new) | Dependencies are current. `scripts/check_deps.py` reports none behind except documented holds, and lower bounds match the locked versions. | Running the script in CI (report only; it fails only if an undocumented dependency is behind). |

## Changes

- **`src/heliopuddle/storage/backends.py`** (new): the object-store protocol table, `default_temp_writes(protocol)` and `native_md5(info)`. These are the only places that know about specific backends.
- **`storage/location.py`**:
  - remove `_RENAME_PROTOCOLS`, `_AZURE_PROTOCOLS`, container creation and `_native_md5`;
  - add `temp_writes` to `StorageLocation` and use a generic `makedirs`;
  - add the clear error for a missing root;
  - `native_md5` and `list_files` call `backends.native_md5`.
- **Callers:** add `temp_writes` to `config/models.py` (`StorageLocationConfig`) and pass it through wherever `StorageLocation` is built:
  - the client, from the storage-location API response (`StorageLocationRead` and `Detail` gain `temp_writes`);
  - the verify, delete, accept and index services.
- **`server/services/accept.py`**: always read the content.
- **`pyproject.toml`**:
  - add the extras;
  - move adlfs out of core;
  - raise lower bounds to the current releases;
  - run `uv lock --upgrade`;
  - FastAPI stays where Prefect allows, with a comment saying so.
- **`docker/Dockerfile`**: install `.[all]`.
- **Tests:**
  - `tests/unit/test_storage_conformance.py` and `tests/integration/test_storage_conformance_azure.py`, sharing one mixin;
  - `tests/unit/test_backends.py`: the table, native md5, the six-protocol smoke test and the source-scan guard;
  - Azure-specific tests updated for G3: fixtures create the container.
- **Docs:**
  - `docs/spec/requirements/storage.md` and `deploy.md`: the new requirements;
  - `features.md`: the M3 section notes generic storage, plus decisions G1–G8;
  - `docs/design/storage.md` (new): the generic fsspec model, `temp_writes`, roots, native md5, supported backends and extras, and how to add a backend;
  - small edits to `architecture.md`, `deployment.md` (the Azure section becomes an example of one backend, with S3, GCS, SFTP and SMB config examples) and `indexing.md`;
  - README: supported backends and extras;
  - CLAUDE.md: storage-generality and dependency-policy rules.

## Build

This is small and tightly coupled, so one agent does the code (storage, callers, extras, tests). I do the docs and the dependency audit first, then review and verify.

## Verification

- `ruff`, `pyright`, `lint-imports` and the source-scan guard.
- Unit tests at ≥90% coverage, integration tests (Azurite conformance, Postgres, Prefect) and Docker e2e (Azurite, extras present).
- `req_matrix.py` for M3: every requirement passes, including STO-06/07 and DEP-08/09.
- `scripts/check_deps.py` shows everything current except the documented FastAPI hold.
- A manual run: `server start` on local storage with an add, an index and the UI, plus the Docker image with Azurite.
- Push to the M3 branch, which updates PR #3, then ask you to test.

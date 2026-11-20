# M3 follow-up 2 (approved): sensible defaults, auto-created roots, zero-config start

## Context

After the generic-storage follow-up, hp refuses to create a storage location's root, and the local demo now needs a manual `mkdir`. You said that creating a local root automatically is fine, and set a general principle: **hp should be easy to set up with minimal config. It assumes sensible defaults during initial setup and lets the user reconfigure them.** This follow-up applies that principle on the M3 branch (PR #3) and records it as a standing rule.

## Decisions

| # | Topic | Decision |
|---|---|---|
| E1 | Standing principle | CLAUDE.md gets a new convention: "Easy to set up: sensible defaults, minimal config; everything has a working default that the user can override." The spec review of each milestone checks new settings against it. |
| E2 | Creating roots | A new per-storage-location setting, `create_root: auto \| true \| false`, defaults to `auto`.<br>• `auto`: directory-style backends (local, SMB, SFTP and any other non-object store) get their root created with `fs.makedirs(root, exist_ok=True)`. Object stores (S3, Azure, GCS) do not, because creating a bucket or container needs extra cloud permissions and choices such as region. hp checks they exist and gives a clear error if not.<br>• `true`: always try to create the root, including buckets and containers.<br>• `false`: never create it; a missing root is an error.<br>• The `auto` choice uses `backends.is_object_store`, so no backend-specific code is added outside `backends.py`. |
| E3 | When the root is checked or created | At startup, for every active storage location, after config loads: the root is created per E2, or startup **fails fast** with the existing `StorageRootMissingError` message naming the storage location and URL. On each write the existing check still runs: a root that disappears later is re-created per E2, or raises the clear error. |
| E4 | Zero-config start | If `HELIOPUDDLE_CONFIG_DIR` is missing or has no project files, hp starts with a built-in project `default` that has one storage location, `primary` = `file://{data_dir}/store`, created automatically. It logs one line saying so and how to configure your own. Adding any project file replaces the default. So `uv run heliopuddle server start` works with no settings at all. |
| E5 | Docs and demo | The README quick start becomes `uv run heliopuddle server start` with no env vars and no `mkdir`. The `examples/projects/demo.yaml` default stays, but its store is created automatically. `storage.md`, `deployment.md` and `features.md` are updated, plus a short "Defaults" table in `deployment.md` listing every setting with its default. |

## Requirements (all due in M3)

| ID | Requirement (detailed) | Verified by |
|---|---|---|
| **STO-08** (new) | **Creating roots.** Each storage location has `create_root: auto \| true \| false`, default `auto`.<br>• With `auto`, a missing root is created for directory-style backends but not for object stores.<br>• `true` always tries to create it; `false` never does.<br>• Roots are checked and created at startup, and a missing root that can't be created stops startup with a message naming the storage location and URL.<br>• A root that goes missing later is re-created per the setting on the next write, or raises `StorageRootMissingError`.<br>• Only `backends.is_object_store` is used to decide, so STO-06 still holds. | Unit tests on local and memory (auto creates, `false` errors, re-created on write), a fake object store with `auto` (not created, clear error), the CLI start fails fast; Azurite integration: `auto` does not create a container, `true` does. |
| **STO-07** (amended) | The conformance suite's missing-root case follows STO-08: `create_root: false` gives a clear error and the root is not created; `auto` and `true` behave as the backend type says. | Conformance suite (memory, local, Azurite). |
| **DEP-10** (new) | **Zero-config start.** With no project files, `server start` uses the built-in `default` project (storage location `primary` at `file://{data_dir}/store`, created automatically) and logs how to configure your own. Any project file present replaces it. A client add works against it. | CLI/unit test with an empty config dir; integration test: start with no config, add a file, see it in the API. |

## Changes

- **`config/models.py`:** add `create_root` to `StorageLocationConfig`.
- **`config/loader.py`:** `load_projects` returns the built-in default project when the config dir is missing or has no project files. It needs the data dir from settings, so it takes it as a parameter or the context builds the default.
- **`storage/location.py`:**
  - `StorageLocation(..., create_root="auto")`;
  - `ensure_root()`, which applies E2 and raises `StorageRootMissingError`;
  - the write path calls it instead of only checking.
- **Startup (`server/start.py` / `context`):** after loading config and syncing it to the DB, call `ensure_root()` for every active storage location, failing fast (exit 2, with the message).
- **Plumbing:** the API's `StorageLocationRead` exposes `create_root`; the client passes it through, so client writes also create a local root.
- **Docs:**
  - CLAUDE.md principle (E1);
  - `storage.md` (roots section rewritten);
  - `deployment.md` (zero-config start, defaults table);
  - README quick start;
  - `features.md` (decisions E1–E5; STO-08 and DEP-10 added to M3);
  - requirements (`storage.md` STO-08, STO-07 amended; `deploy.md` DEP-10);
  - the review record `docs/spec/reviews/m3-followup2-plan.md`.
- **Tests:** as in the table, and the e2e and integration fixtures can drop their own container and root creation where `create_root: true` is simpler. Azurite tests keep at least one `auto` case to prove object-store roots are not created.

## Build

This is small, so one agent builds it from a brief. I write the docs first, then review and verify.

## Verification

- `ruff`, `pyright`, `lint-imports` and the STO-06 guard.
- Unit tests at ≥90% coverage, then the integration and Docker e2e suites.
- The requirement matrix for M3 is OK, and `scripts/check_deps.py` is OK.
- Manual:
  1. `uv run heliopuddle server start` in an empty directory with no env vars: the `default` project is up, a client add is `available`, and the UI shows it.
  2. The demo config with a new `HELIOPUDDLE_DEMO_DATA` path: the root is created automatically.
  3. A storage location with `create_root: false` and a missing root: startup fails with a clear message.
- Push to the M3 branch (PR #3) and ask you to test.

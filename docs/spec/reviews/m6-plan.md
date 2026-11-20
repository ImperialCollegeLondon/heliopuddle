# M6 spec review and build plan (approved)

## Context

The UI grew page by page: one project page held the storage-location panel,
reconcile form, filters and file list; rules and file details hung off it.
With several projects, several storage locations each and tens of thousands
of files it did not hold together. M6 redesigns the UI as one app, and adds a
search API that works across projects.

Three concepts were mocked up (A Mission Control, B Orbital, C Observatory).
The owner chose C, revised as **C2**: the light Observatory look, no command
palette but a plain file search, and concept A's Files page (views, filter
chips, a state column per storage location, an inspector) restyled in C.

## Decisions

See "M6 review decisions" in [features.md](../features.md) (U1–U8). Calls the
owner made during review:

| Topic | Decision |
|---|---|
| Milestone | The redesign is M6; the old M6–M8 become M7–M9 |
| Design | C2 (Observatory revised), with A's Files page |
| Search | A new API searching across every project the caller has permission on |
| Storage locations | 1–5 per project is typical, not a hard limit |
| JavaScript | Only the basics of each page must work without it |
| Search index, Activity page, % replicated, metadata filter | As recommended: plain `LIKE`; link to Prefect; dropped; not in M6 |
| Branch | `m6-ui` from `main` |

## Requirements

| ID | Summary |
|---|---|
| API-09 (amended) | `state` may be repeated (any of) |
| API-16 | `GET /search/files` across readable projects |
| API-17 | `GET /projects/{p}/summary` |
| CLI-11 | `search()`, `project_summary()`, `find(state=[...])` |
| UI-01, 02, 04, 06, 07, 08 (amended) | Same content, on the new pages |
| UI-09 | Shell, theme, accessibility, no-JS basics |
| UI-10 | Projects home |
| UI-11 | Project Overview |
| UI-12 | Files with views, chips, per-location columns, inspector |
| UI-13 | Storage pages |
| UI-14 | Search box, results page, typeahead |
| API-18, CLI-12, UI-15 | File previews (added during PR review) |
| UI-11 (amended) | Heatmap leaves out all-zero state columns (added during PR review) |

Full descriptions: [api.md](../requirements/api.md),
[client.md](../requirements/client.md), [ui.md](../requirements/ui.md).
Design: [ui.md](../../design/ui.md).

## Build plan

1. Dependencies upgraded (`uv lock --upgrade`, `check_deps.py`);
   `pyproject.toml` milestone `M6`.
2. Schemas for search and summary, shared by API and client.
3. API and client (API-09, API-16, API-17, CLI-11), test-first.
4. UI (UI-01 to UI-14), test-first against stub clients.
5. Integration: Postgres search speed and summary counts.
6. E2E: Playwright screenshots of every page at desktop and phone widths,
   typeahead, inspector, one run with JavaScript off.

## Test strategy

- **Unit:** golden datasets for search ranking and summary counts; the role ×
  project matrix for search; stub-client tests for every UI page and action.
- **Integration:** 200k files across three projects on Postgres, search page
  within 500 ms; summary on Postgres.
- **E2E:** the Docker image with Playwright: every page, the typeahead, the
  inspector, and the basics with JavaScript disabled.

## Screenshots

Home ([m6-home.png](m6-home.png)), Overview ([m6-overview.png](m6-overview.png)),
Files with the inspector ([m6-files-inspector.png](m6-files-inspector.png)),
file page ([m6-file-page.png](m6-file-page.png)), Storage
([m6-storage.png](m6-storage.png)), Rules ([m6-rules.png](m6-rules.png)),
typeahead ([m6-typeahead.png](m6-typeahead.png)), phone width
([m6-files-phone.png](m6-files-phone.png)) and without JavaScript
([m6-no-javascript.png](m6-no-javascript.png)); file previews of a CSV
([m6-preview-csv.png](m6-preview-csv.png)) and a CDF
([m6-preview-cdf.png](m6-preview-cdf.png)).

# UI requirements (`UI`)

From M5A the UI requires a login and shows only what the caller's roles allow
([AUTH-16](auth.md)). From M6 the UI follows the page structure and design in
[design/ui.md](../../design/ui.md); requirements first written for earlier
milestones were amended in M6 to name the page that now carries them.

**UI-01 (M1, amended M6)** `GET /` is the projects home (UI-10); with exactly
one readable project it redirects to that project's Overview. The project's
Files page (UI-12) lists files with path, size, state per storage location,
created time and created by, with paging. Every page links to the Prefect UI
(`HELIOPUDDLE_PREFECT_UI_URL`, derived from `PREFECT_API_URL` by default)
from the header's "Runs" link.
*Verify:* FastAPI test client; e2e Playwright screenshot.

**UI-05 (M1)** The UI reads data only through `heliopuddle.client`, never the DB
or `server.models`. The import-linter contract in CI covers
`heliopuddle.server.ui`.
*Verify:* `lint-imports` in CI.

**UI-03 (M1)** The UI can be served three ways: (a) on the API's port at `/`
(default); (b) on its own port (`HELIOPUDDLE_UI_HOST`/`HELIOPUDDLE_UI_PORT`) in
the same process; (c) as a separate process with `--no-api --no-services`,
talking to `HELIOPUDDLE_UI_API_URL`. `HELIOPUDDLE_UI_ROOT_PATH` and
`HELIOPUDDLE_API_ROOT_PATH` set root paths for reverse proxies.
*Verify:* integration test for each mode, plus one with root paths.

**UI-02 (M2, amended M6)** The file page `/projects/{p}/files/{id}` shows path,
size, hash, version, content date, created and updated by, deleted date,
metadata (escaped), the state of each copy per storage location, and the
version history of its series (newest first, latest marked, the current file
highlighted, deleted versions greyed out, each linking to its page). Paths in
the Files table link to it (with JavaScript they open the inspector, UI-12).
An unknown id, or a file of another project, is a `404` page.
*Verify:* unit tests with a stub client; e2e Playwright screenshot.

**UI-06 (M2, amended M6)** The Files page has filters for a path glob, latest
only, state (one or more) and include deleted, as GET query parameters that
work without JavaScript; paging keeps them and the page shows them back as
removable filter chips. The table shows each file's version and a latest
badge. An unknown state gives a `400` page.
*Verify:* unit tests.

**UI-07 (M3, amended M6)** The Storage page of each storage location (UI-13)
shows its URL (or "Restricted" when AUTH-07 withholds it), whether external
changes are accepted, last full and incremental index times, and "Index"
buttons (full, incremental) that post a run and link to it in Prefect; the
Overview's Actions menu offers the same per storage location. The file page
and the inspector have an "Accept as is" button for each `unknown` copy of a
live file. Live files with no `pending` or `available` copy are labelled "not
latest-eligible". The buttons are plain HTML `POST` forms that work without
JavaScript and redirect back (post/redirect/get); a cross-site post (by
`Sec-Fetch-Site`, `Origin` or `Referer`) is rejected with a `403` page. API
errors render the error page.
*Verify:* unit tests with a stub client.

**UI-04 (M4, amended M6A)** The Files table, the inspector and the file page
show each copy's storage-location state and the ignored flag with a toggle.
`/projects/{p}/rules` shows a data-flow diagram of the rules between storage
locations and, per rule, validated config, effective enabled state, schedule
and watermark; enable/disable buttons; "Run now" with mode/dry run; a Prefect
deployment link; and a one-off run form with every rule field, including match
filters (from M6A an older-than and a younger-than input for each of
`modified`, `created`, `content_date` and `arrived`), options and batch
limits; rule descriptions name every age criterion. Deployment links resolve to actual
deployment UUIDs and use the UI's public Prefect URL when configured. One-off
runs are full, single-batch and unsaved; their form identifies
retrigger/pause as ignored. All controls use the existing protected
POST/redirect/GET pattern and the HTTP-only client. Successful run submission
redirects to a notice linking to the submitted Prefect flow run.
*Verify:* unit tests with stub clients and Docker/Playwright e2e.

**UI-08 (M5, amended M6)** The project Overview's Actions menu has a
"Reconcile" form: a verify checkbox and a rule multi-select (all selected by
default). It is a protected POST/redirect/GET form through the HTTP-only
client; on success the page shows a notice linking to the submitted Prefect
flow run, and API errors (`409` unfinished reconcile, `422`) render the error
page. The form is shown only to an operator on that project (or an admin);
the API enforces the same permission even for a forged POST.
*Verify:* unit test with a stub client; e2e test of the button.

**UI-09 (M6)** Every page shares one shell: a dark starfield header with the
logo, a project switcher (projects the caller can read), the project tabs
Overview, Files, Storage and Rules, a "Runs" link to the Prefect UI, a file
search box (UI-14) and a user menu with the username, roles and log out. The
content uses the light theme of [design/ui.md](../../design/ui.md): text meets
WCAG AA contrast, copy states use a symbol as well as a colour, and pages lay
out without horizontal page scrolling at phone width (wide tables scroll in
their own box). Fonts, icons, CSS and JavaScript are served from the UI's
`static/` directory (no CDN). The basics of every page work without
JavaScript: its content, links, GET filters and paging, and POST action
forms. Enhancements may need JavaScript: typeahead search, the in-page
inspector and the column chooser. Write controls are hidden from callers
without the role (AUTH-16).
*Verify:* unit tests of the shell and role-dependent controls; e2e
screenshots at desktop and phone widths, and one run with JavaScript off.

**UI-10 (M6)** The projects home `/` shows a card per readable project (live
files, bytes, its storage locations and an attention badge from API-17) and a
"Needs attention" list across those projects, each entry linking to the
project's Files page with the matching view and storage location.
*Verify:* unit tests with a stub client.

**UI-11 (M6)** The project Overview `/projects/{p}` shows key figures (live
files, latest files, bytes, needs attention, in progress), a copy-state
heatmap with a row per storage location and a column per state (from API-17;
a state, or "no copy", that is zero in every row gets no column) whose every
non-zero cell links to the Files page filtered by that storage
location and state, a "Needs attention" list, a rules summary (name, action,
schedule, enabled, last success) linking to the Rules page, and, for
operators, an Actions menu with Reconcile (UI-08) and Index per active
storage location (UI-07).
*Verify:* unit tests with a stub client.

**UI-12 (M6)** The Files page `/projects/{p}/files` has view tabs with counts:
All, Latest, Needs attention (`unknown` or `missing` copy), In progress
(`pending`, `overwriting` or `deleting` copy), Ignored by rules and Deleted.
Filters (UI-06, plus storage location and content date range) show as
removable chips. The table has a state column per storage location; with more
than six, the first six in config order are columns, a column chooser
(repeatable `col=` query parameter) picks others, and the inspector and file page
always show every copy. Rows per page are 25, 50, 100 or 200. `?file={id}`
renders the inspector beside the table: badges, Download, Ignore/Include in
rules, copies with "Accept as is", details, version history and metadata;
with JavaScript, choosing a row opens the inspector without a page reload.
*Verify:* unit tests with a stub client; e2e test of views, filters and the
inspector.

**UI-13 (M6)** `/projects/{p}/storage` lists the project's storage locations
and `/projects/{p}/storage/{name}` shows one: URL (or "Restricted"), backend,
active flag, read-only config, index times and Index buttons (UI-07), file
and byte counts, a copy-state bar whose segments link to the Files page
filtered to that storage location and state, and the rules that read from or
write to it, each linking to the Rules page. An unknown name is a `404` page.
*Verify:* unit tests with a stub client.

**UI-14 (M6)** The header search box searches files by path through API-16:
on the home page across every readable project, on project pages within that
project (with a link to widen to all projects). Submitting goes to
`/search?q=&project=` showing results grouped by project, each linking to
the file, with paging; this works without JavaScript. With JavaScript, typing
shows the top results as you type, from an HTML fragment the UI renders
through the client.
*Verify:* unit tests with a stub client; e2e typeahead test.

**UI-15 (M6)** The file page has a "Preview" section built from API-18
through the client: tables with a sticky header in their own scrolling box,
column types where known, and "first 100 of N rows" (or "first 100 rows" when
N is unknown); file-level attributes (CDF global attributes, Parquet metadata)
and, for CDF, each table's variables with their attributes in collapsible
lists; JSON pretty-printed; text in a monospaced block; Markdown rendered with
raw HTML escaped and unsafe links dropped; images shown inline from a UI route
that streams the bytes through the client (at most 20 MiB, served with
`Content-Security-Policy: sandbox` and `nosniff`). Formats without a preview,
files too large to preview and unreadable files show the API's message. The
inspector links to the preview. The preview loads after the rest of the page
when JavaScript is on, and is rendered in the page without it.
*Verify:* unit tests with a stub client for every kind; e2e test with a CSV
and a CDF file.

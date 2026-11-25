# UI design

The UI is a FastAPI + Jinja2 app in `heliopuddle.server.ui`. It reads and
writes only through `heliopuddle.client` (UI-05), renders HTML on the server,
and adds a little vanilla JavaScript for enhancements. This page describes the
page structure (M6), the visual system, how JavaScript is used, and the two API
endpoints added for the UI (search and summary).

## Page structure

One app for many projects. Everything below the home page is scoped to one
project, chosen in the header's project switcher.

| Page | URL | Shows |
|---|---|---|
| Projects home | `/` | A card per readable project (files, bytes, storage locations, attention badge) and a "Needs attention" list across projects. One readable project redirects to its Overview. |
| Overview | `/projects/{p}` | Key figures, copy-state heatmap (storage location × state; cells link to Files), needs attention, rules summary, recently added files, Actions menu (Reconcile, Index per storage location). |
| Files | `/projects/{p}/files` | View tabs (`view=`) with counts, filter chips, a table with a state column per storage location (`col=`, repeatable), paging, the inspector (`?file={id}`). Below 1600px wide the inspector hides the "Created by" column. |
| Inspector | `/projects/{p}/files/{id}/inspector` | The inspector as an HTML fragment, for the in-page swap. |
| Download | `/projects/{p}/files/{id}/download` | Streams the file's content through `client.open()`. |
| File | `/projects/{p}/files/{id}` | The full file page: badges, actions, copies, details, a content preview ([preview](preview.md)), version history, metadata. |
| Preview | `/projects/{p}/files/{id}/preview`, `/projects/{p}/files/{id}/raw` | The preview as an HTML fragment (loaded after the page with JavaScript; without it the file page's "Show preview" link renders it in the page with `?preview=1`), and an image's bytes for `<img>`. |
| Storage | `/projects/{p}/storage`, `/projects/{p}/storage/{name}` | Storage-location list and a page per storage location: config, index times and buttons, copy-state bar, rules touching it. An incomplete storage location (STO-18) gets a warning banner on both pages listing its problems, with a link to Prefect's `/blocks` page when the Prefect UI URL is known (plain HTML, no JavaScript). |
| Rules | `/projects/{p}/rules` | Data-flow diagram, rules table with enable/disable and Run (each row shows the complete selection and options, wrapped, never truncated), one-off run form. |
| Search | `/search?q=&project=` | Search results grouped by project (API-16). |
| Login, error | `/login`, error pages | As in M5A. |

URLs from before M6 are not kept. Each POST action redirects to the page
that shows its result (Overview for reconcile, the storage-location page for
index, the file or Files page for accept and ignore, Rules for rule runs).
Accept and ignore carry `return_to` and `return_query` so they come back to
the exact Files view they were made from.

### Shell

`base.html` renders the header on every page:

- a dark band with a CSS starfield (layered `radial-gradient` dots, no
  images) and a faint solar glow at the right;
- logo (a half sun over two reflected lines) and the word "heliopuddle";
- breadcrumb "Projects / {project ▾}": the switcher is a `<details>` menu of
  readable projects, so it works without JavaScript;
- the file search box (`GET /search`, UI-14), scoped to the current project
  on project pages;
- "Runs ↗" to the Prefect UI, when known;
- the user menu (`<details>`): username, roles, log out;
- on the login page only the logo: no Projects or Runs links, which need a
  signed-in user;
- on project pages, the tabs Overview, Files, Storage and Rules, the current
  one underlined in the accent colour, and an Actions menu (`<details>`) for
  operators.

### Scaling

Typical projects have 1–5 storage locations and tens of thousands of files;
more must still work:

- Files shows at most six storage-location columns (the first six active,
  in config order, by default). The column chooser sets `col=a&col=b…`
  (a checkbox form without JavaScript); with JavaScript it builds
  `columns=a,b`, and `columns=-` means no state columns (every box can be
  unticked). The inspector and file page always list every copy.
- The heatmap and the Storage list grow by rows; heatmap state columns that
  are zero in every row are left out.
- The rules diagram is laid out in columns by flow depth (a storage location
  that only rules read from sits left, each destination one column right of
  its deepest source), nodes stacked within a column, so any number of
  storage locations and rules fit; it scrolls horizontally inside its card if
  wider than the page.
- Lists page on the server (`limit`/`offset`), never load whole tables, and
  counts come from API-17 instead of counting pages.

## Visual system

Light content under a dark header. Tokens live as CSS custom properties in
`static/hp.css`:

| Token | Value | Use |
|---|---|---|
| `--bg` | `#F5F6FA` | page background |
| `--surface` | `#FFFFFF` | cards, tables |
| `--surface-2` | `#FAFBFD` | table heads, inspector |
| `--border` | `#E1E4EC` | card and table borders |
| `--ink` | `#0E1324` | text, primary buttons |
| `--muted` | `#5B6478` | secondary text (≥ 4.5:1 on white) |
| `--accent` | `#FF8A3D` | logo, current tab, current-version marker |
| `--link` | `#3438C9` | links |
| `--night` | `#070A18` → `#121736` | header gradient |

Copy states each have a background, a foreground and a symbol, so colour is
never the only cue:

| State | Symbol | Background / text |
|---|---|---|
| available | ✓ | `#DDF5EA` / `#0B6B45` |
| pending | ◌ | `#E1E6FF` / `#2B33B8` |
| overwriting | ↻ | `#E1E6FF` / `#2B33B8` |
| unknown | ? | `#FFEBD9` / `#9A3D00` |
| missing | ! | `#FFE0E3` / `#A3122A` |
| deleting | × | `#EEE6FF` / `#5A2BB0` |
| deleted, no copy | – | `#F0F2F7` / `#5B6478` |

Type: Manrope (headings, figures) and IBM Plex Sans (text), IBM Plex Mono
(paths, hashes, numbers), all OFL fonts bundled as woff2 under
`static/fonts/`. Radius 14px for cards and 9px for controls; 8px spacing
grid; controls at least 36px high (44px touch targets for the main actions).

Layout breakpoints: below 900px the inspector stacks under the table and
grids become one column; below 600px the header wraps (search full width) and
the tabs scroll horizontally.

## JavaScript

The basics of every page work without JavaScript: content, links, GET filters
and paging, and POST action forms. `static/hp.js` (vanilla, no build step,
no framework) adds:

- **typeahead**: typing in the search box (debounced, 2+ characters) fetches
  `/search/suggest?q=&project=`, an HTML fragment the UI renders through the
  client, and shows it under the box; arrow keys and Enter choose a result;
- **inspector**: in the Files table, choosing a row fetches
  `/projects/{p}/files/{id}/inspector` (an HTML fragment) and swaps it in,
  updating the URL's `file=` with `history.replaceState`; without JavaScript
  the path link goes to the file page;
- **file preview**: on the file page, fetches the Preview fragment
  (`data-preview`) after the page has loaded and swaps it into the card;
- **column chooser**: builds `columns=` from the checkbox menu when its Apply
  button is pressed (ticking boxes changes nothing until then, except that the
  seventh box is disabled while six are ticked);
- small conveniences: closing `<details>` menus on outside click, confirm
  prompts on delete-like actions.

## API support (M6)

### Search: `GET /search/files` (API-16)

Projects searched: the caller's readable projects (`has_any_role_on`), or
those named in `project=`, each of which must be readable (else `404`, the
same as for an unknown project). The model function takes the list of project
names, so the API layer owns authorization.

Matching: `q` is split on whitespace into words; each word becomes
`file.path_lower LIKE '%' || escaped(lower(word)) || '%' ESCAPE '\'`, all
ANDed, plus the project filter and the `deleted_at`/`is_latest` filters of
API-09. The rank is one `CASE` expression over the first word against
`file.name_lower`: 0 equal, 1 starts with, 2 contains, 3 otherwise; order is
`rank, created_at DESC, id DESC`. Counts per project come from one
`GROUP BY project` query with the same conditions; `total` is their sum.
The project names are resolved to ids first, so neither scan joins
`project`; the page is ranked over ids only (a narrow top-N sort) and its
files are then loaded by id.
`path_lower` and `name_lower` are generated columns (stored
on Postgres, virtual on SQLite), so the scans compare stored text instead of
lower-casing every row twice: on 200k files this cut the worst case by about
40%. There is no special index: the scan is bounded by the readable projects
and checked by the 200k-row integration test (500 ms, best of three runs per
case). If that ever fails, a Postgres `pg_trgm` index is the next step.

### Summary: `GET /projects/{p}/summary` (API-17)

Three aggregate queries per request, no caching:

1. live files: `count(*)`, `count(*) FILTER (is_latest)`, `sum(size)`, and
   the deleted count;
2. copies of live files grouped by `(storage_location_id, state)`, plus
   per storage location the count and size sum of files with a non-`deleted`
   copy (`no_copy` is live files minus that count);
3. view counts with `EXISTS` subqueries over copy states, and the ignored
   count.

`FILTER` is written as `sum(case when … then 1 else 0 end)` so it runs on
SQLite and Postgres alike.

## Templates and files

**Convention.** Each page has its own folder under `pages/` that holds
everything only that page uses: its routes, its helpers and its templates.
Code or templates used by two or more pages go in `shared/` (code) or
`templates/` (templates). Modules stay short (a few hundred lines at most); a
page that grows is split by concern, as `pages/files/` is.

```
server/ui/
  __init__.py         public exports: create_ui, ClientFactory, default_api_url, ...
  app.py              create_ui: app, session, static mount, Jinja environment,
                      page routers, error handlers (no routes of its own)
  shared/             used by several pages
    context.py        UiContext (settings, client factory, templates), kept on
                      app.state.ui; render, error_page, client_for, url,
                      project_shell, files_link; the `Ui` route dependency
    clients.py        ClientFactory, default_api_url, the default factory
    forms.py          form parsing, same-origin (CSRF) check, form_bool,
                      the Files return query
    errors.py         BadRequestError, CrossSiteError, exception handlers
    templating.py     Jinja loader (templates/, pages/, auth/), shared filters
    format.py         sizes, counts, times, durations, paths, names
    states.py         copy-state sets and styles
    fileinfo.py       version label, health, latest-eligible, downloadable
    summary.py        view counts, attention items, heatmap/bar cells
    rules_text.py     one-line rule route and match summaries
    links.py          files_url
  auth/               login (M5A)
    session.py        signed session cookie, SessionUser, guard
    oidc.py           PKCE, external URLs, provider cache
    routes.py         /login, /logout, /auth/callback
    login.html
  pages/
    home/             `/`: routes.py, health.py, home.html
    overview/         Overview and reconcile: routes.py, heatmap.py, overview.html
    files/            Files list, inspector, file page, preview, raw, download,
                      accept, rules-ignore
      __init__.py     router (the modules below, in route order) and the
                      page's template filters
      listing.py  inspector.py  detail.py  fragment.py  content.py  actions.py
      filters.py  tabs.py  paging.py  lookup.py  preview.py
      files.html  _inspector.html  file.html  _preview.html
    storage/          routes.py, bar.py, storage.html, storage_location.html
    rules/            page.py, actions.py, one_off.py, flow.py (diagram layout),
                      rules.html, _rules_flow.html
    search/           routes.py, search.html, _suggest.html
  templates/          base.html (shell), _macros.html (state badge, chips,
                      pager, buttons, cards), error.html
  static/
    hp.css  hp.js  icon.svg  fonts/ (woff2 + OFL licences)
```

A page package exposes `router` (an `APIRouter`). Route functions take the
shared state through a dependency, `ui: Ui` (reads `request.app.state.ui`),
not through closures. Route names (`ui_files`, ...), paths and registration
order are part of the contract: templates use `path_for(name)`. Jinja finds a
page's template by folder, as `files/files.html`; every template is shipped in
the wheel and the Docker image with the rest of `heliopuddle`.

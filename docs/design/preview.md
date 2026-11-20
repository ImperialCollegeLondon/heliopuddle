# File previews

The file page shows a preview of the file's content (UI-15). The API builds it
(`GET /files/{id}/preview`, API-18) because only the server holds storage
credentials for every user, and the UI must talk to the API only through the
client. Files can be hundreds of MB, so every format reads only what it needs.

## Where the code lives

```
heliopuddle/preview/          pure readers: (open file or local path) -> FilePreview parts
  __init__.py                 build_preview(name, opener, ...) and format dispatch
  text.py                     csv/tsv, txt/log, md, json, jsonl
  parquet.py                  pyarrow
  cdf.py                      crump (bounded reads, grouping, column names)
  cells.py                    cell conversion and limits
  streams.py                  byte-limiting reader; storage-failure guard
server/services/preview.py    picks the copy, opens it, caches results
server/api/files/preview.py   GET /files/{id}/preview
client/_content.py            preview() (HelioPuddleClient)
server/ui/pages/files/        preview fragment, image route, Markdown rendering
```

`heliopuddle.preview` knows nothing about the database or storage locations: it
gets a file name, a function that opens the content for reading, the size,
and (when the storage location has one) a local path. That keeps it
unit-testable with plain files.

Every stream it opens is wrapped in a guard that turns any exception from the
storage into `StorageReadError` (a `FileNotFoundError` passes through) and
remembers it, so a parser that wraps the failure in its own error type is
still reported as a storage failure (`503`) and not as kind `error`.

## Choosing and opening the copy

As for `GET /files/{id}/content`: the first configured copy in `available`
state, else `overwriting` (which still holds the original). The service opens
it with the storage location's `open_read`; for CDF it asks
`storage/backends.py` for a local path (`local_path(fs, path)`, the only
backend-aware step, returning `None` for remote filesystems).

## Formats

| Format | How it is read | Bound |
|---|---|---|
| CSV, TSV | `io.TextIOWrapper` over the stream (UTF-8, replacement on errors), `csv.Sniffer` on the first 64 KiB, then `csv.reader` | 101 rows (to know whether more exist), at most 4 MiB |
| text, log | lines from the stream | 100 lines, at most 4 MiB |
| Markdown | the start of the stream | 256 KiB |
| JSON | `ijson` streams the top level: an array yields items, an object yields key-value pairs | 101 items or entries, at most 4 MiB |
| JSON lines | one `json.loads` per line | 101 lines, at most 4 MiB |
| image | not read; the UI streams the bytes | the UI refuses images over 20 MiB |
| Parquet | `pyarrow.parquet.ParquetFile` over the seekable stream: schema and key-value metadata and `num_rows` from the footer, then `iter_batches(batch_size=100)` over only the leading row groups that hold the first 100 rows and the first 100 columns, without pre-buffering | the footer plus those row groups' columns |
| CDF | crump 0.8: `read_cdf_variables(local_path, max_records=100, skipped=...)` and `read_cdf_global_attributes`; `cdflib` `varinq` for CDF types and the record-varying flag | 100 records per variable |

The 4 MiB cap is enforced by a reader that stops after that many bytes, so a
single huge line cannot exhaust memory either; a table or text cut short by
it says so.

### CDF tables

crump does the reading (from 0.8): `read_cdf_variables(path, max_records=100,
skipped=...)` reads at most 100 records of each variable, keeps each
variable's true record count and its dtype, turns epochs into datetimes
(shown as ISO timestamps), and lists unreadable variables (including ones with
no records) in `skipped` instead of failing. heliopuddle groups the
record-varying variables with crump's `group_variables_by_record_count` (one
table per record count, largest first) and names a 2-D variable's columns with
crump's `CDFVariable.get_column_names` (from its `LABL_PTR_1`/label
attributes). Non-record-varying variables form their own one-row table:
crump's grouping keys a variable by the length of its data, which for a
non-record-varying array is its element count, so heliopuddle separates them
by cdflib's `Rec_Vary` flag first. Skipped variables are named in the
preview's `message`, shown above the tables. Tables are named after their record count
(`5 records`, ...) and `non-record-varying`. A variable with records of more
than one dimension stays one column, each cell the record as a JSON array
string. Label variables pointed to by `LABL_PTR_1` are read one record at most. Each table lists its variables with type, shape,
record count and attributes; global attributes go in `attributes`. Attribute
values that are lists or arrays are joined into one string.

A CDF on storage with no local path is streamed into a temporary file first
(deleted afterwards), only if its size is at most
`HELIOPUDDLE_PREVIEW_MAX_BYTES` (default `1GB`); larger ones give kind
`too_large` with a message saying so.

## Limits and safety

- At most 100 columns per table (`hidden_columns` counts the rest), 1,000
  characters per cell, 100 rows.
- Cells are JSON scalars: numpy and Arrow values are converted; dates, bytes,
  nested values and non-finite floats become strings.
- Parse failures give kind `error` with a short message (the exception type
  and text, no paths or credentials); storage failures are `503`.
- Results are cached in an in-process LRU of 64 previews keyed on (database,
  file id, storage location, size, mtime), so reopening a page is instant and
  a changed copy is read again. The database URL in the key only keeps
  separate app contexts in one process (tests) apart.
- A JSON array or JSON lines file whose items are not all objects gets a
  `value` column for the other items. Bytes cells show printable UTF-8 as
  text and anything else with `\x..` escapes. CSV cells stay strings.

## UI

The file page renders the preview section from the fragment route
`/projects/{p}/files/{id}/preview` (template `files/_preview.html`, helpers in
`server/ui/pages/files/preview.py`). The page itself never waits for the preview: its
Preview card holds a placeholder with `data-preview` set to the fragment URL,
which `hp.js` fetches after the page has loaded and swaps in. Without
JavaScript the placeholder's "Show preview" link loads the page again with
`?preview=1#preview`, and the server then renders the same fragment inside the
page. A file without a readable copy (deleted, or no `available` or
`overwriting` copy) shows a notice and the API is not asked. API errors from
the preview become a notice inside the card, not an error page.

Tables sit in their own scrolling box with a sticky header and a sticky row
number column; each column header shows the column type, when known, in small
muted mono. Numeric columns (every shown value a number) are right-aligned in
mono, nulls show as a muted "—", and a caption under the table says "first 100
of N rows", "first 100 rows" when N is unknown, or "N rows", plus how many
columns were left out. Several tables (a CDF) are collapsible blocks titled
like "Table 1 · 86,400 records", the first one open; each lists its variables
(name, type, shape, records, and the attributes in a nested `<details>`).
CDF global attributes and Parquet key-value metadata are a collapsible
"Global attributes" / "File attributes" list above the tables. JSON is a mono
block with light colouring of keys, strings, numbers and literals (a small
lexer that escapes everything, so text cut off mid-value still renders). The
messages of kinds `none`, `too_large` and `error` are calm notices with a
Download button. The Files inspector has a "Preview" link to the file page's
`#preview` anchor. Markdown is rendered with `markdown-it-py` in CommonMark
mode plus its table and strikethrough rules, with HTML disabled (raw HTML is
escaped) and only `http`, `https`,
`mailto` and relative links kept. Images use `/projects/{p}/files/{id}/raw`,
which streams the file through `client.open()` with the image media type
(PNG, JPEG, GIF, WebP, AVIF, BMP, SVG by extension; other files are `404`),
`Content-Security-Policy: sandbox; default-src 'none'; style-src
'unsafe-inline'` and `X-Content-Type-Options: nosniff` (an SVG opened directly
cannot run scripts or load anything), refusing files over 20 MiB with a `413`
page; the fragment shows a notice instead of the image for those.

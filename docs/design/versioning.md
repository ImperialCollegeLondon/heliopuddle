# Versioning

A file's series and version come from its **path** through per-path rules in
the project config. A client may instead give the version itself when it
creates the file (see [Overrides](#overrides)); the series still comes from the
path.

A file has either a **number** version (`file.version_number`, an integer), a
**text** version (`file.version_text`), or none (it is unversioned). It never
has both: a database `CHECK` enforces that at most one is set.

## Rules

```yaml
versioning:                         # first matching rule wins
  - paths: ["calibration/**"]       # glob(s) on the relative path
    version_type: text
    extraction_pattern: '_v(?P<major>\d{1,3})\.(?P<minor>\d{1,4})\.(?P<ext>[^./]+)$'
    version_text: '{major:0>3}.{minor:0>4}'
  - paths: ["**"]
    version_type: number            # the default
    extraction_pattern: '_[vV](?P<version>\d{1,5})\.[^./]+$'
```

| Field | Default | Meaning |
|---|---|---|
| `paths` | required | non-empty list of globs (`*`, `**`, `?`) matched against the whole relative path |
| `version_type` | `number` | `number` or `text` |
| `extraction_pattern` | required | Python regex, searched (`re.search`) in the **file name** |
| `version_text` | none | `text` rules only: a Python format string over the pattern's named groups |
| `ignore_in_series` | `[]` | named groups that do not split a series (for example `[ext]`) |

A rule whose `paths` match but whose pattern does not is skipped, and the next
rule is tried. If no rule matches, the file is **unversioned**.

### The default rule

When a project does not set `versioning`, it gets one rule:

```yaml
versioning:
  - paths: ["**"]
    version_type: number
    extraction_pattern: '_[vV](?P<version>\d{1,5})\.[^./]+$'
```

It versions `file_v8.csv`, `file_V09.csv` and `file_v10.csv` as 8, 9 and 10. It
does not match dates or frame numbers (`a_20250101.csv`, `img_001.png`), so
those are not folded into one series. A project that sets `versioning`
replaces the default rule. `versioning: []` turns versioning off.

## Extracting the version

### `number` rules

The version is the `version` named group if the pattern has one, else the
whole match. It must be ASCII digits and fit a signed 64-bit integer; leading
zeros do not matter (`v09` is 9). Both examples below version `file_12.csv` as
12:

```yaml
  - paths: ["**"]
    extraction_pattern: '_(?P<version>\d{1,10})\.[^./]+$'   # a named group
  - paths: ["**"]
    extraction_pattern: '[0-9]+(?=\.[^./]+$)'               # the whole match
```

### `text` rules

With `version_text`, the version is `version_text.format(**groups)`. Every
named group is passed as a **string**; a group that did not take part in the
match is `""`. Without `version_text`, the version is the `version` group, or
else the whole match. The result must be 1–255 characters long.

Text versions are compared **as strings** (byte order). Pad numbers to a fixed
width in the template so string order is version order:

```yaml
  # file_v001.1234.csv, file_v2.12.csv, file_v0.0001.csv
  # -> "001.1234", "002.0012", "000.0001"
  - paths: ["**"]
    version_type: text
    extraction_pattern: '_v(?P<major>\d{1,3})\.(?P<minor>\d{1,4})\.(?P<ext>[^./]+)$'
    version_text: '{major:0>3}.{minor:0>4}'
```

`{major:0>3}` pads with zeros to width 3. Format specs that need a number
(`{major:03d}`) fail validation, because the values are strings.

### Errors

A `number` value that is not digits or is too large, and a `text` value that
is empty or longer than 255 characters, raise `InvalidVersionError`. Register
and reserve then answer `422` naming the rule index and the value. The indexer
records the file as unversioned (in its literal-path series) with an `unknown`
copy, logs a warning and goes on.

## Content date

If the register request has no `content_date` and the matching rule has a
`date` named group, the date is parsed as `YYYY-MM-DD` or `YYYYMMDD` (UTC
midnight). An unparseable date is ignored, and the file is still registered.

## Series key

Files are versions of each other when their paths are equal after the
**version parts** of the name are replaced by placeholders. The version parts
are:

- the `version` group; or, for a `text` rule with `version_text`, the groups
  the template uses; or, when there are neither, the whole match;
- plus the groups listed in `ignore_in_series`.

The `date` group is never a version part: files with different dates are
different series. Validation rejects `date` in `version_text` and in
`ignore_in_series`. When a version part contains the date (a whole-match
version such as `20250101_v1`, or a `version` group around a `date` group),
only the parts on either side of the date are replaced, so
`x_20250101_v1.csv` has the key `x_20250101{v}.csv`, not `x_{v}.csv`.

Each version part is replaced with a placeholder: `{v}` for the `version`
group or the whole match, and `{<name>}` for any other group. A group nested
inside another version part is covered by the outer one. A group that did not
take part in the match is left alone.

An extension is part of the series by default: a different extension makes a
different series, because the extension (or an `ext` group) stays in the key.
List `ext` in `ignore_in_series` to make all extensions one series.

| Path | Rule | Series key | Version |
|---|---|---|---|
| `sci/a_20250101_v002.cdf` | default | `sci/a_20250101_v{v}.cdf` | number 2 |
| `sci/a_20250101_v002.csv` | default | `sci/a_20250101_v{v}.csv` | number 2 (another series) |
| `spice/k_07.bsp` | `'_(?P<version>\d{2})\.[^./]+$'` | `spice/k_{v}.bsp` | number 7 |
| `x/file_12.csv` | `'[0-9]+(?=\.[^./]+$)'` | `x/file_{v}.csv` | number 12 |
| `cal/x_v1.4.json` | text, `'{major:0>3}.{minor:0>4}'` | `cal/x_v{major}.{minor}.json` | text `001.0004` |
| `cal/x_v1.4.csv` | same, `ignore_in_series: [ext]` | `cal/x_v{major}.{minor}.{ext}` | text `001.0004` |
| `notes/readme.txt` | none | `notes/readme.txt` | unversioned |

To order versions by minor only and ignore the major version, use a `number`
rule whose `version` group is the minor part, and put the major group in
`ignore_in_series`:
`extraction_pattern: '_v(?P<major>\d+)\.(?P<version>\d+)\.[^./]+$'` with
`ignore_in_series: [major]`.

### Series key namespace

Generated keys are prefixed `v:` and literal (unversioned) paths `p:`, so a
generated key such as `v:x_v{v}.dat` never collides with an unversioned file
literally named `x_v{v}.dat` (`p:x_v{v}.dat`). API responses show the key
without the prefix.

## One kind per series

A series holds number versions or text versions, never both.
`series.version_type` (`number`, `text`, or NULL while the series has no
versioned file) is set by the first versioned file and never changes. A later
file of the other kind is rejected: register and reserve answer `422` naming
the series' kind, and the indexer records the file as unversioned (literal
series) with an `unknown` copy and a logged reason.

Unversioned files may share a series with versioned ones: for example, a path
that matches no rule is re-added with a version override after its
unversioned file was removed. They rank below every version.

## Overrides

Register (`POST /projects/{p}/files`) and upload reserve accept either
`version_number` (an integer ≥ 0) or `version_text` (1–255 characters), but not
both (`422`). On a **new** file the override replaces the version derived from
the rules; the series key is still derived from the path (the literal path when
no rule matches). An override of the wrong kind for the series gives `422`.

The client's `add_file` and `register_file` take the same keyword arguments,
and `heliopuddle file add` takes `--version-number` or `--version-text`.

A version is fixed when the file is created. Re-registering or overwriting a
live path keeps its version: an override equal to the stored version, or no
override, is accepted; a different override gives `409`. No endpoint changes
a version.

## Ordering

Within a series, versions are ordered by the series' kind only:

- `number`: the highest `version_number`;
- `text`: the highest `version_text`, compared byte-wise (`COLLATE "C"` on
  Postgres; SQLite compares bytes by default).

Unversioned rows rank lowest. Equal versions (for example `a_v1.cdf` and
`a_v01.cdf`, which share a series) are ordered by `created_at`, newest first,
then by id. Because a series never mixes kinds, the query sorts one indexed
column (`(series_id, version_number DESC)` or `(series_id, version_text
DESC)`), never an expression over both.

The version is stored on the file row and the kind on the series row, so
ordering never needs the config at query time.

`is_latest` is about **live**, **eligible** files only; eligibility is defined
in [data model](data-model.md#is_latest). A new upload becomes latest only once
it is verified (`available`).

The series and version are derived once, when a path is first registered.
Re-registering, overwriting or re-indexing a live path keeps its series and
version even if the rules have changed since. Moving existing files to a new
series after a rule change is not supported.

## Validation

Rules are validated when config is loaded. An invalid rule stops startup,
naming the file, rule index and field:

- `extraction_pattern` must compile;
- `version_type` is `number` or `text`;
- `version_text` is allowed only on `text` rules. Each field must name a group
  of the pattern (not `date`), and a trial format with every group set to
  `"0"` must succeed;
- `ignore_in_series` names groups of the pattern (not `date`);
- `paths` is a non-empty list of non-empty globs;
- unknown keys (including the old `pattern` and `latest_by`) are rejected.

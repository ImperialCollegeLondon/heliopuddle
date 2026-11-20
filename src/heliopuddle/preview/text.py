"""Previews of text formats: CSV/TSV, plain text, Markdown, JSON and JSON lines.

Each reader reads at most :data:`TEXT_LIMIT` bytes from the start of the
stream (Markdown :data:`MARKDOWN_LIMIT`), through a :class:`LimitedReader`, so
a single huge line cannot exhaust memory either.
"""

from __future__ import annotations

import codecs
import csv
import io
import itertools
import json
from collections.abc import Iterator
from typing import Any, BinaryIO

import ijson

from heliopuddle.preview.cells import MAX_ROWS, Content, make_table
from heliopuddle.preview.streams import LimitedReader, limited_text
from heliopuddle.server.schemas.preview import PreviewColumn, PreviewTable

TEXT_LIMIT = 4 * 1024 * 1024
"""Bytes read at most from the start of a text-format file."""

MARKDOWN_LIMIT = 256 * 1024
"""Bytes of Markdown source shown at most."""

SNIFF_CHARS = 64 * 1024
"""Characters given to ``csv.Sniffer``."""

CUT_MESSAGE = "Only the first 4 MiB of the file were read."

_DELIMITERS = ",\t;|"
_WHITESPACE = b" \t\r\n"
_BOM = codecs.BOM_UTF8


def preview_csv(stream: BinaryIO, size: int | None, *, tab: bool = False) -> Content:
    """A table of the header and first 100 rows; the delimiter is sniffed.

    Args:
        stream: the file content.
        size: the file size, if known.
        tab: default to tabs when sniffing fails (``.tsv``).

    Raises:
        csv.Error: the content is not delimited text.
    """
    text, limiter = limited_text(stream, TEXT_LIMIT, size)
    head = text.read(SNIFF_CHARS)
    if head and not head.endswith(("\n", "\r")):
        head += text.readline()
    sample = head[:SNIFF_CHARS]
    if (end := sample.rfind("\n")) > 0:
        sample = sample[:end]  # whole lines only
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=_DELIMITERS).delimiter
    except csv.Error:
        delimiter = "\t" if tab else ","
    reader = csv.reader(itertools.chain(io.StringIO(head, newline=""), text), delimiter=delimiter)
    rows: list[list[str]] = []
    reached_end = True
    try:
        for row in reader:
            rows.append(row)
            if len(rows) > MAX_ROWS + 1:
                reached_end = False
                break
    except csv.Error:
        if not limiter.cut:
            raise
        broken = True  # the row the limit cut through did not parse
    else:
        broken = False
    cut = limiter.cut and reached_end
    if cut and not broken and len(rows) > 1:
        rows.pop()  # the row the limit cut through
    header = rows[0] if rows else []
    body = rows[1:]
    width = max((len(r) for r in rows), default=0)
    columns = [
        PreviewColumn(name=header[i] if i < len(header) and header[i] else f"column {i + 1}")
        for i in range(width)
    ]
    table = make_table(columns, body, more_rows=len(body) > MAX_ROWS or cut)
    return Content(kind="table", tables=[table], message=CUT_MESSAGE if cut else None)


def preview_lines(stream: BinaryIO, size: int | None) -> Content:
    """The first 100 lines of a plain text or log file.

    Args:
        stream: the file content.
        size: the file size, if known.
    """
    text, limiter = limited_text(stream, TEXT_LIMIT, size)
    lines: list[str] = []
    while len(lines) < MAX_ROWS and (line := text.readline()):
        lines.append(line)
    more = len(lines) == MAX_ROWS and bool(text.read(1))
    cut = limiter.cut and not more
    return Content(
        kind="text",
        text="".join(lines),
        text_truncated=more or cut,
        message=CUT_MESSAGE if cut else None,
    )


def preview_markdown(stream: BinaryIO, size: int | None) -> Content:
    """The Markdown source, at most :data:`MARKDOWN_LIMIT` bytes of it.

    Args:
        stream: the file content.
        size: the file size, if known.
    """
    limiter = LimitedReader(stream, MARKDOWN_LIMIT, size)
    data = limiter.readall()
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    text = decoder.decode(data.removeprefix(_BOM), final=not limiter.cut)
    return Content(kind="markdown", text=text, text_truncated=limiter.cut)


def _records_table(items: list[Any], more: bool) -> PreviewTable:
    """A table of JSON values: object keys become columns, other values a ``value`` column."""
    keys: dict[str, None] = {}
    scalars = False
    for item in items[:MAX_ROWS]:
        if isinstance(item, dict):
            keys.update(dict.fromkeys(str(k) for k in item))  # pyright: ignore[reportUnknownVariableType]
        else:
            scalars = True
    names = list(keys)
    value_column = "value" if "value" not in keys else "(value)"
    columns = ([PreviewColumn(name=value_column)] if scalars else []) + [
        PreviewColumn(name=n) for n in names
    ]
    rows: list[list[Any]] = []
    for item in items[:MAX_ROWS]:
        lead: list[Any] = []
        if isinstance(item, dict):
            if scalars:
                lead = [None]
            rows.append(lead + [item.get(n) for n in names])  # pyright: ignore[reportUnknownMemberType]
        else:
            rows.append([item] + [None] * len(names))
    return make_table(columns, rows, more_rows=more or len(items) > MAX_ROWS)


def _first_byte(buffered: io.BufferedReader[Any]) -> bytes:
    """Skip a BOM and whitespace; return the next byte without consuming it (b"" at the end)."""
    if buffered.peek(3).startswith(_BOM):
        buffered.read(3)
    while True:
        peeked = buffered.peek(1)[:1]
        if peeked not in _WHITESPACE or not peeked:
            return peeked
        buffered.read(1)


def _take(iterator: Iterator[Any], count: int, limiter: LimitedReader) -> tuple[list[Any], bool]:
    """Take up to ``count`` values; return them and whether the read limit cut the input."""
    taken: list[Any] = []
    try:
        for value in iterator:
            taken.append(value)
            if len(taken) == count:
                break
    except ijson.JSONError:
        if not limiter.cut:
            raise
        return taken, True
    return taken, False


def preview_json(stream: BinaryIO, size: int | None) -> Content:
    """A table of a top-level array's first 100 items, else pretty-printed JSON.

    A top-level object shows its first 100 entries; a top-level scalar itself.

    Args:
        stream: the file content.
        size: the file size, if known.

    Raises:
        ijson.JSONError: the content is not JSON.
    """
    limiter = LimitedReader(stream, TEXT_LIMIT, size)
    buffered = io.BufferedReader(limiter, buffer_size=64 * 1024)
    first = _first_byte(buffered)
    if not first:
        raise ValueError("the file is empty")
    if first == b"[":
        items, cut = _take(ijson.items(buffered, "item", use_float=True), MAX_ROWS + 1, limiter)
        return Content(
            kind="table",
            tables=[_records_table(items, cut)],
            message=CUT_MESSAGE if cut else None,
        )
    if first == b"{":
        entries, cut = _take(ijson.kvitems(buffered, "", use_float=True), MAX_ROWS + 1, limiter)
        shown: Any = dict(entries[:MAX_ROWS])
        more = len(entries) > MAX_ROWS
    else:
        values, cut = _take(ijson.items(buffered, "", use_float=True), 1, limiter)
        if not values:
            raise ValueError("the JSON value is cut off at the read limit")
        shown, more = values[0], False
    return Content(
        kind="json",
        text=json.dumps(shown, indent=2, ensure_ascii=False, default=str),
        text_truncated=more or cut,
        message=CUT_MESSAGE if cut else None,
    )


def preview_jsonl(stream: BinaryIO, size: int | None) -> Content:
    """A table of the first 100 JSON lines (blank lines are skipped).

    Args:
        stream: the file content.
        size: the file size, if known.

    Raises:
        ValueError: a line (other than one cut by the read limit) is not JSON.
    """
    text, limiter = limited_text(stream, TEXT_LIMIT, size)
    items: list[Any] = []
    cut = False
    number = 0
    while len(items) <= MAX_ROWS and (line := text.readline()):
        number += 1
        if not line.strip():
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError as error:
            if limiter.cut and not text.read(1):
                cut = True
                break
            raise ValueError(f"line {number}: {error}") from None
    else:
        cut = limiter.cut and len(items) <= MAX_ROWS
    return Content(
        kind="table",
        tables=[_records_table(items, cut)],
        message=CUT_MESSAGE if cut else None,
    )

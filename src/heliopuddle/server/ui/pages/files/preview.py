"""Presentation of file previews (UI-15): Markdown, JSON colouring, tables and images.

The API builds the preview (API-18, docs/design/preview.md); this module only
turns a :class:`~heliopuddle.server.schemas.FilePreview` into safe HTML pieces.
Everything that reaches a template as :class:`~markupsafe.Markup` is escaped or
sanitised here first.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from functools import lru_cache
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from markdown_it import MarkdownIt
from markupsafe import Markup, escape

from heliopuddle.server.schemas import FilePreview, PreviewTable

__all__ = [
    "IMAGE_TYPES",
    "MAX_IMAGE_BYTES",
    "cell_text",
    "highlight_json",
    "image_media_type",
    "numeric_columns",
    "render_markdown",
    "rows_caption",
    "safe_link",
    "table_title",
]

#: Images larger than this are not shown inline or served by the raw route.
MAX_IMAGE_BYTES = 20 * 1024 * 1024

#: Image extensions a browser shows in ``<img>``, with their media types.
IMAGE_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
}

_SAFE_SCHEMES = frozenset({"http", "https", "mailto"})


def image_media_type(name: str) -> str | None:
    """The media type of an image file name, or None if it is not a shown image format."""
    return IMAGE_TYPES.get(PurePosixPath(name).suffix.lower())


# --- Markdown ------------------------------------------------------------------------------------


def safe_link(url: str) -> bool:
    """Whether a Markdown link target is kept: ``http``, ``https``, ``mailto`` or relative.

    Args:
        url: the link target as written (markdown-it has normalised it).

    Returns:
        True for the allowed schemes and for links without a scheme.
    """
    cleaned = "".join(ch for ch in url.strip() if ch.isprintable() and not ch.isspace())
    try:
        scheme = urlsplit(cleaned).scheme.lower()
    except ValueError:
        return False
    if not scheme:
        return ":" not in cleaned.split("/", 1)[0]
    return scheme in _SAFE_SCHEMES


@lru_cache(maxsize=1)
def _markdown() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
    md.validateLink = safe_link  # type: ignore[method-assign]
    return md


def render_markdown(text: str) -> Markup:
    """Render Markdown as HTML: CommonMark plus tables, raw HTML escaped, only safe links kept.

    Args:
        text: the Markdown source.

    Returns:
        HTML safe to insert into a page.
    """
    return Markup(_markdown().render(text))


# --- JSON ----------------------------------------------------------------------------------

_JSON_TOKEN = re.compile(
    r'(?P<str>"(?:[^"\\\n]|\\.)*")(?P<colon>\s*:)?'
    r"|(?P<num>-?\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b)"
    r"|(?P<lit>\b(?:true|false|null)\b)"
)


def highlight_json(text: str) -> Markup:
    """Light syntax colouring for pretty-printed JSON: keys, strings, numbers, literals.

    It is a lexer, not a parser, so text cut off mid-value still renders.

    Args:
        text: JSON text.

    Returns:
        Escaped HTML with ``<span class="j-…">`` around tokens.
    """
    out: list[str] = []
    pos = 0
    for match in _JSON_TOKEN.finditer(text):
        out.append(str(escape(text[pos : match.start()])))
        if match.group("str") is not None:
            css = "j-key" if match.group("colon") else "j-str"
            out.append(f'<span class="{css}">{escape(match.group("str"))}</span>')
            out.append(str(escape(match.group("colon") or "")))
        elif match.group("num") is not None:
            out.append(f'<span class="j-num">{match.group("num")}</span>')
        else:
            out.append(f'<span class="j-lit">{match.group("lit")}</span>')
        pos = match.end()
    out.append(str(escape(text[pos:])))
    return Markup("".join(out))


# --- tables -------------------------------------------------------------------------------------


def _number(value: int) -> str:
    return f"{value:,}"


_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")


def _is_number(value: str | int | float | bool) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int | float):
        return True
    return _NUMBER.fullmatch(value.strip()) is not None


def numeric_columns(table: PreviewTable) -> list[bool]:
    """Per column, whether every shown value is a number (shown right-aligned in mono).

    Text formats such as CSV give strings, so a string that reads as a number
    counts as one. Nulls and empty strings are skipped; a column with no values
    counts as numeric when its type says so.
    """
    numeric: list[bool] = []
    for index, column in enumerate(table.columns):
        values = [
            v
            for row in table.rows
            if index < len(row) and (v := row[index]) is not None and v != ""
        ]
        if values:
            numeric.append(all(_is_number(v) for v in values))
        else:
            kind = (column.type or "").lower()
            numeric.append(any(word in kind for word in ("int", "float", "double", "real")))
    return numeric


def cell_text(value: str | int | float | bool | None) -> str:
    """A cell as text; booleans in JSON spelling. None is drawn by the template."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else str(value)


def rows_caption(table: PreviewTable) -> str:
    """How many rows are shown: "first 100 of 86,400 rows", "first 100 rows" or "3 rows"."""
    shown = len(table.rows)
    if table.more_rows:
        if table.total_rows is not None:
            return f"first {_number(shown)} of {_number(table.total_rows)} rows"
        return f"first {_number(shown)} rows"
    total = table.total_rows if table.total_rows is not None else shown
    return f"{_number(total)} {'row' if total == 1 else 'rows'}"


_COUNT_NAME = re.compile(r"\d+ (?:records?|rows?)")


def table_title(table: PreviewTable, index: int, preview: FilePreview) -> str:
    """A table's heading: its name (or "Table N") and its size, e.g. "Table 1 · 86,400 records"."""
    name = table.name or ""
    if not name or _COUNT_NAME.fullmatch(name):  # a name like "86400 records" adds nothing
        name = f"Table {index}"
    total = table.total_rows if table.total_rows is not None else None
    if total is None and not table.more_rows:
        total = len(table.rows)
    if total is None:
        return name
    unit = "record" if preview.format == "cdf" else "row"
    return f"{name} · {_number(total)} {unit}{'' if total == 1 else 's'}"


def shape_text(shape: Sequence[int]) -> str:
    """A variable's per-record shape: ``scalar``, ``[3]`` or ``[4×8]``."""  # noqa: RUF002
    if not shape:
        return "scalar"
    return "[" + "×".join(str(n) for n in shape) + "]"  # noqa: RUF001

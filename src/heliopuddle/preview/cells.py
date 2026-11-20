"""Cell conversion and table limits for previews (API-18).

Every cell sent to a client is a JSON scalar of bounded size: numpy and Arrow
values become Python scalars; dates, bytes, nested values and non-finite
floats become strings; strings are cut to :data:`MAX_CELL_CHARS`.
"""

from __future__ import annotations

import datetime as dt
import decimal
import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from heliopuddle.server.schemas.preview import (
    Cell,
    PreviewColumn,
    PreviewKind,
    PreviewTable,
    PreviewVariable,
)

MAX_ROWS = 100
"""Rows shown per table."""

MAX_COLUMNS = 100
"""Columns shown per table; ``hidden_columns`` counts the rest."""

MAX_CELL_CHARS = 1000
"""Characters kept per cell (and per attribute value)."""

_ELLIPSIS = "…"


@dataclass
class Content:
    """What a format reader found: the content fields of a ``FilePreview``."""

    kind: PreviewKind
    tables: list[PreviewTable] = field(default_factory=list)
    text: str | None = None
    text_truncated: bool = False
    attributes: dict[str, str] = field(default_factory=dict)
    message: str | None = None


def clip(text: str, limit: int = MAX_CELL_CHARS) -> str:
    """Cut ``text`` to at most ``limit`` characters, marking a cut with an ellipsis.

    Args:
        text: any string.
        limit: the maximum length of the result.
    """
    return text if len(text) <= limit else text[: limit - 1] + _ELLIPSIS


def _bytes_text(data: bytes) -> str:
    """Printable UTF-8 as itself; anything else with escapes (as Python writes bytes)."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    if text is not None and all(c.isprintable() or c in "\t\r\n" for c in text):
        return text
    return repr(data)[2:-1]


def short_float(value: np.floating[Any]) -> float:
    """A narrow float (float16/float32) as the shortest decimal that round-trips at its width.

    ``np.float32(1.7279210).item()`` is ``1.7279210090637207``; this returns
    ``1.727921``, which is what the file holds.

    Args:
        value: a numpy floating scalar.

    Returns:
        A Python float.
    """
    if value.dtype.itemsize >= 8 or not np.isfinite(value):
        return float(value)
    return float(np.format_float_positional(value, unique=True, trim="-"))


def _jsonable(value: Any) -> Any:
    """Turn nested numpy/Arrow-ish values into plain JSON-serialisable Python."""
    if isinstance(value, np.ndarray):
        if np.issubdtype(value.dtype, np.floating) and value.dtype.itemsize < 8:
            return [_jsonable(v) for v in value]  # numpy scalars, kept narrow
        return [_jsonable(v) for v in value.tolist()]
    if isinstance(value, np.floating):
        return _jsonable(short_float(value))  # pyright: ignore[reportUnknownArgumentType]
    if isinstance(value, np.generic):
        return _jsonable(value.item())
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return to_cell(value)


def to_cell(value: Any) -> Cell:
    """Convert one value to a bounded JSON scalar.

    Args:
        value: a Python, numpy or Arrow-converted value.

    Returns:
        ``None``, a bool, an int, a finite float or a string of at most
        :data:`MAX_CELL_CHARS` characters.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, np.floating):
        return to_cell(short_float(value))  # pyright: ignore[reportUnknownArgumentType]
    if isinstance(value, np.generic) and not isinstance(value, np.datetime64 | np.timedelta64):
        return to_cell(value.item())
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    return _text_cell(value)


def _text_cell(value: Any) -> Cell:
    """Render a non-numeric value as a clipped string cell."""
    if isinstance(value, str):
        return clip(value)
    if isinstance(value, decimal.Decimal):
        return clip(str(value))
    if isinstance(value, bytes | bytearray | memoryview):
        return clip(_bytes_text(bytes(value)[: MAX_CELL_CHARS * 4]))
    if isinstance(value, dt.datetime | dt.date | dt.time):
        return clip(value.isoformat())
    if isinstance(value, np.datetime64 | np.timedelta64 | dt.timedelta):
        return clip(str(value))
    if isinstance(value, np.ndarray | list | tuple | dict):
        try:
            return clip(json.dumps(_jsonable(value), ensure_ascii=False, default=str))
        except (TypeError, ValueError):
            return clip(str(value))  # pyright: ignore[reportUnknownArgumentType]
    return clip(str(value))


def attribute_text(value: Any) -> str:
    """Render an attribute value as one bounded string; lists and arrays are joined.

    Args:
        value: an attribute value (scalar, list or numpy array).
    """
    if isinstance(value, np.ndarray):
        value = list(value.flat)  # numpy scalars, so narrow floats keep their precision
    if isinstance(value, list | tuple):
        return clip(", ".join(attribute_text(v) for v in value))  # pyright: ignore[reportUnknownVariableType]
    cell = to_cell(value)
    return "" if cell is None else clip(str(cell))


def attributes_text(attributes: dict[str, Any]) -> dict[str, str]:
    """Render every value of an attribute mapping with :func:`attribute_text`."""
    return {str(name): attribute_text(value) for name, value in attributes.items()}


def make_table(
    columns: Sequence[PreviewColumn],
    rows: Iterable[Sequence[Any]],
    *,
    more_rows: bool,
    name: str | None = None,
    total_rows: int | None = None,
    variables: Sequence[PreviewVariable] = (),
) -> PreviewTable:
    """Build a table, applying the column, row and cell limits.

    Rows shorter than the columns are padded with ``None``; rows are cut to
    the shown columns.

    Args:
        columns: all columns of the table.
        rows: at most :data:`MAX_ROWS` rows of raw values (extra rows are dropped
            and set ``more_rows``).
        more_rows: the source has rows after these.
        name: the table name.
        total_rows: all rows in the table, when known.
        variables: CDF variables the columns come from.
    """
    shown = list(columns[:MAX_COLUMNS])
    width = len(shown)
    out: list[list[Cell]] = []
    for row in rows:
        if len(out) == MAX_ROWS:
            more_rows = True
            break
        cells = [to_cell(v) for v in row[:width]]
        cells.extend([None] * (width - len(cells)))
        out.append(cells)
    return PreviewTable(
        name=name,
        columns=shown,
        rows=out,
        total_rows=total_rows,
        more_rows=more_rows,
        hidden_columns=len(columns) - width,
        variables=list(variables),
    )

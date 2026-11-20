"""Parquet previews: the footer, then only the row groups that hold the first 100 rows."""

from __future__ import annotations

from typing import Any, BinaryIO

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from heliopuddle.preview.cells import (
    MAX_COLUMNS,
    MAX_ROWS,
    Content,
    attribute_text,
    make_table,
    short_float,
)
from heliopuddle.server.schemas.preview import PreviewColumn

_ARROW_SCHEMA_KEY = b"ARROW:schema"  # Arrow's serialised schema: binary noise for a reader


def preview_parquet(stream: BinaryIO) -> Content:
    """Schema types, key-value metadata, total rows and the first 100 rows.

    Only the footer and the first 100 columns of the leading row groups that
    hold the first 100 rows are read.

    Args:
        stream: the seekable file content.

    Raises:
        Exception: pyarrow's error when the content is not Parquet.
    """
    parquet = pq.ParquetFile(stream, pre_buffer=False)
    metadata = parquet.metadata
    schema = parquet.schema_arrow
    columns = [PreviewColumn(name=field.name, type=str(field.type)) for field in schema]
    shown = list(range(min(len(columns), MAX_COLUMNS)))
    row_groups: list[int] = []
    covered = 0
    for index in range(metadata.num_row_groups):
        if covered >= MAX_ROWS:
            break
        row_groups.append(index)
        covered += metadata.row_group(index).num_rows
    rows: list[list[Any]] = []
    if shown and row_groups:
        names = [schema.names[i] for i in shown]
        batches = parquet.iter_batches(
            batch_size=MAX_ROWS, row_groups=row_groups, columns=names, use_threads=False
        )
        batch = next(iter(batches), None)
        if batch is not None:
            values = [_column_values(batch.column(i)) for i in range(batch.num_columns)]
            rows = [list(row) for row in zip(*values, strict=True)]
    attributes = {
        key.decode("utf-8", "replace"): attribute_text(value.decode("utf-8", "replace"))
        for key, value in (metadata.metadata or {}).items()
        if key != _ARROW_SCHEMA_KEY
    }
    total = metadata.num_rows
    table = make_table(columns, rows, more_rows=total > len(rows), total_rows=total)
    return Content(kind="table", tables=[table], attributes=attributes)


def _column_values(column: Any) -> list[Any]:
    """A column's values as Python objects, keeping float32/float16 at their own precision."""
    if pa.types.is_float32(column.type) or pa.types.is_float16(column.type):
        width = np.float32 if pa.types.is_float32(column.type) else np.float16
        return [None if v is None else short_float(width(v)) for v in column.to_pylist()]
    return column.to_pylist()

"""File previews of Parquet and cell rendering (API-18)."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pyarrow as pa
import pytest

from heliopuddle.preview.cells import MAX_CELL_CHARS, to_cell
from tests.unit._preview_formats_helpers import Generated, _parquet, preview, rows

pytestmark = pytest.mark.req("API-18")


def test_parquet_schema_metadata_rows_and_row_groups() -> None:
    n = 100_000
    table = pa.table(
        {
            "time": pa.array(
                [datetime(2020, 1, 1, tzinfo=UTC)] * n, type=pa.timestamp("us", tz="UTC")
            ),
            "value": pa.array([float(i) for i in range(n)]),
            "flag": pa.array([i % 2 == 0 for i in range(n)]),
            "vec": pa.array([[i, i + 1] for i in range(n)], type=pa.list_(pa.int32())),
            "raw": pa.array([b"\x00\x01"] * n),
        }
    ).replace_schema_metadata({"origin": "sample", "version": "3"})
    data = _parquet(table, row_group_size=10_000)
    stream = Generated(data, b"\0", len(data))
    p = preview("t.parquet", stream, size=len(data))
    assert (p.kind, p.format) == ("table", "parquet")
    assert p.attributes == {"origin": "sample", "version": "3"}
    t = p.tables[0]
    assert [(c.name, c.type) for c in t.columns] == [
        ("time", "timestamp[us, tz=UTC]"),
        ("value", "double"),
        ("flag", "bool"),
        ("vec", "list<element: int32>"),
        ("raw", "binary"),
    ]
    assert (t.total_rows, t.more_rows, len(t.rows)) == (n, True, 100)
    assert t.rows[0] == ["2020-01-01T00:00:00+00:00", 0.0, True, "[0, 1]", "\\x00\\x01"]
    assert t.rows[99][1] == 99.0
    # Only the footer and the first of ten row groups were read.
    assert stream.bytes_read < len(data) / 5


def test_parquet_rows_span_small_row_groups() -> None:
    p = preview("s.parquet", _parquet(pa.table({"x": list(range(200))}), row_group_size=30))
    assert rows(p) == [[i] for i in range(100)]
    assert (p.tables[0].total_rows, p.tables[0].more_rows) == (200, True)


def test_parquet_wide_table_hides_columns() -> None:
    table = pa.table({f"c{i}": [i, i] for i in range(120)})
    p = preview("w.parquet", _parquet(table))
    t = p.tables[0]
    assert (len(t.columns), t.hidden_columns, len(t.rows[0])) == (100, 20, 100)
    assert (t.total_rows, t.more_rows) == (2, False)


def test_parquet_non_finite_and_null_cells() -> None:
    table = pa.table({"x": [math.nan, math.inf, None, 1.5]})
    p = preview("n.parquet", _parquet(table))
    assert rows(p) == [["nan"], ["inf"], [None], [1.5]]


def test_damaged_parquet_is_an_error() -> None:
    p = preview("bad.parquet", b"PAR1 this is not parquet PAR1")
    assert p.kind == "error"
    assert p.message


@pytest.mark.parametrize(
    ("value", "cell"),
    [
        (None, None),
        (True, True),
        (np.int64(5), 5),
        (np.float32(0.5), 0.5),
        (np.bool_(False), False),
        (math.nan, "nan"),
        (-math.inf, "-inf"),
        (np.float64("nan"), "nan"),
        (datetime(2020, 1, 2, 3, 4, 5), "2020-01-02T03:04:05"),
        (np.datetime64("2020-01-02T03:04:05.000000001"), "2020-01-02T03:04:05.000000001"),
        (b"abc", "abc"),
        (np.array([[1, 2], [3, 4]]), "[[1, 2], [3, 4]]"),
        ({"a": [1.5, math.nan]}, '{"a": [1.5, "nan"]}'),
    ],
)
def test_cells_are_json_scalars(value: Any, cell: Any) -> None:
    assert to_cell(value) == cell


def test_long_cells_and_arrays_are_clipped() -> None:
    assert len(str(to_cell("x" * 5000))) == MAX_CELL_CHARS
    assert len(str(to_cell(np.arange(10_000)))) == MAX_CELL_CHARS

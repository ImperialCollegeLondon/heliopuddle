"""File previews of images and delimited text (API-18)."""

from __future__ import annotations

from typing import Any

import pytest

from heliopuddle.preview import build_preview, format_of
from heliopuddle.preview.cells import MAX_CELL_CHARS
from heliopuddle.preview.text import TEXT_LIMIT
from tests.unit._preview_formats_helpers import BIG, GB, Generated, names, preview, rows

pytestmark = pytest.mark.req("API-18")


@pytest.mark.parametrize(
    ("name", "media_type"),
    [
        ("a/plot.png", "image/png"),
        ("x.JPG", "image/jpeg"),
        ("x.jpeg", "image/jpeg"),
        ("x.gif", "image/gif"),
        ("x.webp", "image/webp"),
        ("x.Svg", "image/svg+xml"),
    ],
)
def test_images_are_not_read_and_carry_their_media_type(name: str, media_type: str) -> None:
    def never() -> Any:
        raise AssertionError("an image preview must not read the file")

    p = build_preview(file_id=1, name=name, opener=never, size=5, max_bytes=GB)
    assert (p.kind, p.media_type, p.file_id) == ("image", media_type, 1)


@pytest.mark.parametrize("name", ["data.bin", "README", "archive.tar.gz", ".csv"])
def test_unknown_extension_gives_none(name: str) -> None:
    p = preview(name, b"a,b\n1,2\n")
    assert p.kind == "none"
    assert p.format is None
    assert p.message


def test_extension_is_case_insensitive() -> None:
    fmt = format_of("dir.v1/DATA.CSV")
    assert fmt is not None
    assert fmt.name == "csv"
    assert preview("dir.v1/DATA.CSV", b"a,b\n1,2\n").kind == "table"


def test_csv_header_and_rows() -> None:
    p = preview("t.csv", b'time,value,label\n1,2.5,a\n2,3.5,"b, c"\n')
    assert (p.kind, p.format, p.storage_location) == ("table", "csv", "primary")
    assert names(p) == ["time", "value", "label"]
    assert rows(p) == [["1", "2.5", "a"], ["2", "3.5", "b, c"]]
    assert p.tables[0].more_rows is False
    assert p.message is None


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("t.tsv", b"a\tb\tc\n1\t2\t3\n4\t5\t6\n"),
        ("t.csv", b"a;b;c\n1;2;3\n4;5;6\n"),
        ("t.csv", b"a|b|c\n1|2|3\n4|5|6\n"),
    ],
)
def test_delimiter_is_sniffed(name: str, data: bytes) -> None:
    p = preview(name, data)
    assert names(p) == ["a", "b", "c"]
    assert rows(p) == [["1", "2", "3"], ["4", "5", "6"]]


def test_single_column_tsv_falls_back_to_tab() -> None:
    p = preview("t.tsv", b"only\n1\n2\n")
    assert names(p) == ["only"]
    assert rows(p) == [["1"], ["2"]]


def test_more_rows_flag_and_row_limit() -> None:
    data = b"n\n" + b"".join(f"{i}\n".encode() for i in range(150))
    p = preview("t.csv", data)
    assert len(rows(p)) == 100
    assert rows(p)[-1] == ["99"]
    assert p.tables[0].more_rows is True
    exact = preview("t.csv", b"n\n" + b"".join(f"{i}\n".encode() for i in range(100)))
    assert len(rows(exact)) == 100
    assert exact.tables[0].more_rows is False


def test_ragged_rows_are_padded_and_extra_cells_get_columns() -> None:
    p = preview("t.csv", b"a,b\n1\n1,2,3,4\n")
    assert names(p) == ["a", "b", "column 3", "column 4"]
    assert rows(p) == [["1", None, None, None], ["1", "2", "3", "4"]]


def test_invalid_utf8_is_replaced_and_bom_dropped() -> None:
    p = preview("t.csv", b"\xef\xbb\xbfname,v\ncaf\xe9,1\n")
    assert names(p) == ["name", "v"]
    assert rows(p) == [["caf\ufffd", "1"]]


def test_columns_beyond_the_limit_are_counted() -> None:
    header = ",".join(f"c{i}" for i in range(130)).encode()
    line = ",".join(str(i) for i in range(130)).encode()
    p = preview("wide.csv", header + b"\n" + line + b"\n")
    table = p.tables[0]
    assert len(table.columns) == 100
    assert table.hidden_columns == 30
    assert len(table.rows[0]) == 100


def test_empty_csv_is_an_empty_table() -> None:
    p = preview("t.csv", b"")
    assert p.kind == "table"
    assert p.tables[0].rows == []
    assert p.tables[0].columns == []


def test_long_cells_are_clipped() -> None:
    p = preview("t.csv", b"a\n" + b"x" * 5000 + b"\n")
    cell = rows(p)[0][0]
    assert isinstance(cell, str)
    assert len(cell) == MAX_CELL_CHARS
    assert cell.endswith("…")


def test_large_csv_reads_only_its_start() -> None:
    stream = Generated(b"time,value\n", b"2020-01-01T00:00:00,1.25\n", BIG)
    p = preview("big.csv", stream, size=BIG)
    assert len(rows(p)) == 100
    assert p.tables[0].more_rows is True
    assert p.message is None
    assert stream.bytes_read <= 256 * 1024


def test_csv_with_a_huge_line_stops_at_the_read_limit() -> None:
    stream = Generated(b"a,b\n1,2\n", b"x", BIG)
    p = preview("huge.csv", stream, size=BIG)
    assert stream.bytes_read <= TEXT_LIMIT
    assert p.kind == "table"
    assert rows(p) == [["1", "2"]]
    assert p.tables[0].more_rows is True
    assert p.message and "4 MiB" in p.message

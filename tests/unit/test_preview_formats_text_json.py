"""File previews of text, Markdown, JSON and JSON Lines (API-18)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from heliopuddle.preview.text import MARKDOWN_LIMIT, TEXT_LIMIT
from tests.unit._preview_formats_helpers import BIG, Generated, names, preview, rows

pytestmark = pytest.mark.req("API-18")


def test_text_shows_the_first_100_lines() -> None:
    data = "".join(f"line {i}\n" for i in range(150)).encode()
    p = preview("run.log", data)
    assert (p.kind, p.format) == ("text", "log")
    assert p.text is not None
    assert p.text.splitlines() == [f"line {i}" for i in range(100)]
    assert p.text_truncated is True
    short = preview("notes.txt", b"one\ntwo\n")
    assert (short.text, short.text_truncated) == ("one\ntwo\n", False)


def test_text_of_exactly_100_lines_is_not_truncated() -> None:
    p = preview("t.txt", "".join(f"{i}\n" for i in range(100)).encode())
    assert p.text_truncated is False


def test_text_with_one_huge_line_reads_at_most_the_limit() -> None:
    stream = Generated(b"", b"abcdefgh", BIG)
    p = preview("huge.txt", stream, size=BIG)
    assert stream.bytes_read <= TEXT_LIMIT
    assert p.text is not None
    assert len(p.text) == TEXT_LIMIT
    assert p.text_truncated is True
    assert p.message and "4 MiB" in p.message


def test_markdown_is_capped_at_256_kib() -> None:
    p = preview("README.md", b"# Title\n\nBody\n")
    assert (p.kind, p.text, p.text_truncated) == ("markdown", "# Title\n\nBody\n", False)
    data = ("é" * 200_000).encode()  # two bytes each: 400 kB
    big = preview("doc.MARKDOWN", data)
    assert big.text is not None
    assert len(big.text.encode()) <= MARKDOWN_LIMIT
    assert big.text_truncated is True
    assert set(big.text) == {"é"}  # a character cut in half is dropped, not mangled


def test_json_array_of_objects_is_a_table_of_its_first_100_items() -> None:
    items: list[dict[str, Any]] = [{"id": i, "v": i / 2} for i in range(150)]
    items[3]["extra"] = {"nested": [1, 2]}
    items[120]["late"] = True  # beyond the first 100 items: not a column
    p = preview("items.json", json.dumps(items).encode())
    assert p.kind == "table"
    assert names(p) == ["id", "v", "extra"]
    assert rows(p)[0] == [0, 0.0, None]
    assert rows(p)[3] == [3, 1.5, '{"nested": [1, 2]}']
    assert len(rows(p)) == 100
    assert p.tables[0].more_rows is True


def test_json_array_of_scalars_has_a_value_column() -> None:
    p = preview("v.json", b'[1, 2.5, "x", null, true, [1]]')
    assert names(p) == ["value"]
    assert rows(p) == [[1], [2.5], ["x"], [None], [True], ["[1]"]]
    assert p.tables[0].more_rows is False


def test_json_mixed_array_keeps_objects_and_scalars() -> None:
    p = preview("v.json", b'[{"a": 1}, 2]')
    assert names(p) == ["value", "a"]
    assert rows(p) == [[None, 1], [2, None]]


def test_json_object_is_pretty_printed_up_to_100_entries() -> None:
    p = preview("cfg.json", b'{"a": 1, "b": [1, 2], "c": {"d": null}}')
    assert p.kind == "json"
    assert p.text is not None
    assert json.loads(p.text) == {"a": 1, "b": [1, 2], "c": {"d": None}}
    assert "\n  " in p.text
    assert p.text_truncated is False
    big = preview("big.json", json.dumps({f"k{i}": i for i in range(150)}).encode())
    assert big.text is not None
    assert list(json.loads(big.text)) == [f"k{i}" for i in range(100)]
    assert big.text_truncated is True


def test_json_scalar_document() -> None:
    p = preview("n.json", b'  "hello"  ')
    assert (p.kind, p.text) == ("json", '"hello"')


@pytest.mark.parametrize(
    "data",
    [
        b'[{"a": 1}, {"a": ',  # truncated
        b'{"a": tru}',  # damaged
        b"not json at all",
        b"",
    ],
)
def test_truncated_or_damaged_json_is_an_error(data: bytes) -> None:
    p = preview("bad.json", data)
    assert p.kind == "error"
    assert p.message
    assert p.tables == []


def test_large_json_array_reads_only_its_start() -> None:
    stream = Generated(b"[", b'{"time": "2020-01-01", "value": 1.5},', BIG)
    p = preview("big.json", stream, size=BIG)
    assert len(rows(p)) == 100
    assert p.tables[0].more_rows is True
    assert stream.bytes_read <= 256 * 1024


def test_json_with_a_huge_value_stops_at_the_read_limit() -> None:
    stream = Generated(b'[{"a": 1}, "', b"x", BIG)
    p = preview("huge.json", stream, size=BIG)
    assert stream.bytes_read <= TEXT_LIMIT
    assert p.kind == "table"
    assert rows(p) == [[1]]  # the cut string is not an item
    assert p.tables[0].more_rows is True
    assert p.message and "4 MiB" in p.message


def test_jsonl_table_skips_blank_lines() -> None:
    p = preview("e.jsonl", b'{"a": 1}\n\n{"a": 2, "b": "x"}\n')
    assert (p.kind, p.format) == ("table", "jsonl")
    assert names(p) == ["a", "b"]
    assert rows(p) == [[1, None], [2, "x"]]
    assert p.tables[0].more_rows is False


def test_ndjson_more_rows() -> None:
    p = preview("e.ndjson", b"".join(f'{{"i": {i}}}\n'.encode() for i in range(101)))
    assert len(rows(p)) == 100
    assert p.tables[0].more_rows is True


def test_jsonl_bad_line_is_an_error_naming_the_line() -> None:
    p = preview("e.jsonl", b'{"a": 1}\n{oops\n')
    assert p.kind == "error"
    assert p.message and "line 2" in p.message


def test_jsonl_line_cut_by_the_read_limit_is_dropped() -> None:
    stream = Generated(b'{"a": 1}\n{"a": "', b"y", BIG)
    p = preview("e.jsonl", stream, size=BIG)
    assert stream.bytes_read <= TEXT_LIMIT
    assert rows(p) == [[1]]
    assert p.tables[0].more_rows is True
    assert p.message

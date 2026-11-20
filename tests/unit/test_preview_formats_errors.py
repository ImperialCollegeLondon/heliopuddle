"""Preview failure modes and float formatting (API-18)."""

from __future__ import annotations

import io
from typing import Any

import numpy as np
import pyarrow as pa
import pytest

from heliopuddle.preview import StorageReadError, build_preview, error_message
from heliopuddle.preview.cells import attribute_text, to_cell
from tests.unit._preview_formats_helpers import GB, _parquet, preview, rows

pytestmark = pytest.mark.req("API-18")


class Broken(io.RawIOBase):
    def __init__(self, error: Exception, good: bytes = b"") -> None:
        super().__init__()
        self.error, self.good = error, good

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        if self.good:
            data, self.good = self.good, b""
            buffer[: len(data)] = data
            return len(data)
        raise self.error


def test_storage_open_failure_is_a_storage_error() -> None:
    def fail() -> Any:
        raise PermissionError("secret-credential")

    with pytest.raises(StorageReadError) as raised:
        build_preview(file_id=1, name="t.csv", opener=fail, size=3, max_bytes=GB)
    assert "secret-credential" not in str(raised.value)


@pytest.mark.parametrize("name", ["t.csv", "t.txt", "t.json", "t.jsonl", "t.md", "t.parquet"])
def test_storage_read_failure_is_not_a_parse_error(name: str) -> None:
    with pytest.raises(StorageReadError) as raised:
        preview(name, Broken(ConnectionError("secret-credential"), b"a"), size=100)
    assert "secret-credential" not in str(raised.value)


def test_file_gone_while_reading_is_not_found() -> None:
    with pytest.raises(FileNotFoundError):
        preview("t.csv", Broken(FileNotFoundError("gone")), size=100)


def test_error_messages_hide_paths() -> None:
    error = OSError("/srv/private/data/x.cdf is not a CDF file or a non-supported CDF!")
    message = error_message(error)
    assert "/srv" not in message
    assert message.startswith("OSError: <file> is not a CDF file")
    assert error_message(ValueError("C:/x"), "C:/x") == "ValueError: <file>"
    assert len(error_message(ValueError("y" * 1000))) == 300


@pytest.mark.req("API-18")
def test_narrow_floats_show_their_own_precision() -> None:
    assert to_cell(np.float32(1.727921)) == 1.727921
    assert to_cell(np.float16(0.1)) == 0.1
    assert to_cell(np.float64(1.7279210090637207)) == 1.7279210090637207
    assert to_cell(np.float32("nan")) == "nan"
    assert to_cell(np.array([np.float32(0.1), np.float32(2.5)])) == "[0.1, 2.5]"
    assert attribute_text(np.array([-1e31], dtype=np.float32)) == "-1e+31"
    table = pa.table(
        {
            "f32": pa.array([1.727921, None], type=pa.float32()),
            "f64": pa.array([0.1, 0.2], type=pa.float64()),
        }
    )
    p = preview("n.parquet", _parquet(table, row_group_size=10))
    assert rows(p) == [[1.727921, 0.1], [None, 0.2]]

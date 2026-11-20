"""CDF previews: tables grouped by record count, bounded reads, temporary copies (API-18)."""

from __future__ import annotations

import tempfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import cdflib
import fsspec
import numpy as np
import pytest
from cdflib import cdfepoch
from cdflib.cdfwrite import CDF as CDFWriter

from heliopuddle.preview import build_preview
from heliopuddle.server.schemas import FilePreview
from heliopuddle.storage import backends

pytestmark = pytest.mark.req("API-18")

GB = 1_000_000_000
CDF_DOUBLE, CDF_INT4, CDF_CHAR, CDF_TT2000 = 45, 4, 51, 33


def _var(name: str, data_type: int, *, dims: list[int] | None = None, vary: bool = True,
         elements: int = 1) -> dict[str, Any]:  # fmt: skip
    return {
        "Variable": name,
        "Data_Type": data_type,
        "Num_Elements": elements,
        "Rec_Vary": vary,
        "Dim_Sizes": dims or [],
        "Compress": 0,
    }


def write_sample(path: Path) -> None:
    """Global attributes; 5-record epoch, 1-D, 2-D (labelled) and 3-D variables; a
    3-record variable; non-record-varying labels and a constant."""
    writer = CDFWriter(str(path), cdf_spec={"Compressed": 0})
    writer.write_globalattrs({"Project": {0: "demo"}, "Sources": {0: "x", 1: "y"}})
    epochs = cdfepoch.compute_tt2000([[2020, 1, 1, 0, 0, s, 0, 0, 0] for s in range(5)])
    writer.write_var(_var("Epoch", CDF_TT2000), {"UNITS": "ns"}, np.asarray(epochs))
    writer.write_var(
        _var("B", CDF_DOUBLE, dims=[3]),
        {"LABL_PTR_1": "B_labels", "UNITS": "nT", "VALIDMIN": [-1.0, -1.0, -1.0]},
        np.arange(15.0).reshape(5, 3),
    )
    density = np.array([1.0, np.nan, 3.0, 4.0, 5.0])
    writer.write_var(_var("density", CDF_DOUBLE), {"DEPEND_0": "Epoch"}, density)
    writer.write_var(
        _var("image", CDF_INT4, dims=[2, 2]), {}, np.arange(20, dtype=np.int32).reshape(5, 2, 2)
    )
    writer.write_var(_var("slow", CDF_INT4), {}, np.array([7, 8, 9], dtype=np.int32))
    writer.write_var(
        _var("B_labels", CDF_CHAR, dims=[3], vary=False, elements=2),
        {},
        np.array(["Bx", "By", "Bz"]),
    )
    writer.write_var(_var("version", CDF_INT4, vary=False), {}, np.array([3], dtype=np.int32))
    writer.close()


@pytest.fixture
def sample(tmp_path: Path) -> Path:
    path = tmp_path / "sample.cdf"
    write_sample(path)
    return path


def preview_local(path: Path) -> FilePreview:
    def never() -> Any:
        raise AssertionError("a local CDF is read by path")

    return build_preview(
        file_id=3, name=path.name, opener=never, size=path.stat().st_size, max_bytes=GB,
        local_path=str(path),
    )  # fmt: skip


def test_cdf_tables_follow_record_counts(sample: Path) -> None:
    p = preview_local(sample)
    assert (p.kind, p.format, p.media_type) == ("table", "cdf", "application/x-cdf")
    assert p.attributes == {"Project": "demo", "Sources": "x, y"}
    assert [t.name for t in p.tables] == ["5 records", "3 records", "non-record-varying"]
    five, three, fixed = p.tables

    assert [c.name for c in five.columns] == [
        "Epoch", "B_Bx", "B_By", "B_Bz", "density", "image",
    ]  # fmt: skip
    assert five.columns[0].type == "CDF_TIME_TT2000"
    assert five.columns[1].type == "CDF_DOUBLE"
    assert (five.total_rows, five.more_rows, len(five.rows)) == (5, False, 5)
    assert five.rows[0] == ["2020-01-01T00:00:00.000000000", 0.0, 1.0, 2.0, 1.0, "[[0, 1], [2, 3]]"]
    assert five.rows[1][4] == "nan"
    assert five.rows[4][1:4] == [12.0, 13.0, 14.0]

    variables = {v.name: v for v in five.variables}
    assert list(variables) == ["Epoch", "B", "density", "image"]
    b = variables["B"]
    assert (b.type, b.shape, b.records) == ("CDF_DOUBLE", [3], 5)
    assert b.attributes == {"LABL_PTR_1": "B_labels", "UNITS": "nT", "VALIDMIN": "-1.0, -1.0, -1.0"}
    assert variables["image"].shape == [2, 2]

    assert [c.name for c in three.columns] == ["slow"]
    assert three.rows == [[7], [8], [9]]
    assert three.total_rows == 3

    assert [v.name for v in fixed.variables] == ["B_labels", "version"]
    assert [c.name for c in fixed.columns] == ["B_labels_0", "B_labels_1", "B_labels_2", "version"]
    assert fixed.rows == [["Bx", "By", "Bz", 3]]
    assert (fixed.total_rows, fixed.more_rows) == (1, False)


def test_cdf_without_labels_uses_generic_column_names(tmp_path: Path) -> None:
    path = tmp_path / "v.cdf"
    writer = CDFWriter(str(path), cdf_spec={"Compressed": 0})
    writer.write_var(_var("field", CDF_DOUBLE, dims=[3]), {}, np.ones((2, 3)))
    writer.write_var(_var("one", CDF_DOUBLE, dims=[2]), {}, np.ones((1, 2)))
    writer.close()
    p = preview_local(path)
    assert [c.name for c in p.tables[0].columns] == ["field_x", "field_y", "field_z"]
    assert p.tables[1].rows == [[1.0, 1.0]]  # a single record keeps its 2-D shape


@pytest.fixture
def varget_calls(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[Any, ...]]]:
    calls: list[tuple[Any, ...]] = []
    original = cdflib.CDF.varget

    def spy(self: cdflib.CDF, variable: Any = None, *args: Any, **kwargs: Any) -> Any:
        calls.append((variable, kwargs.get("startrec", 0), kwargs.get("endrec")))
        return original(self, variable, *args, **kwargs)

    monkeypatch.setattr(cdflib.CDF, "varget", spy)
    yield calls


def test_cdf_with_a_million_records_reads_only_the_first_100(
    tmp_path: Path, varget_calls: list[tuple[Any, ...]]
) -> None:
    path = tmp_path / "big.cdf"
    writer = CDFWriter(str(path), cdf_spec={"Compressed": 0})
    writer.write_var(_var("counts", CDF_INT4), {}, np.arange(1_000_000, dtype=np.int32))
    writer.write_var(_var("flux", CDF_DOUBLE, dims=[2]), {}, np.ones((1_000_000, 2)))
    writer.close()
    started = time.perf_counter()
    p = preview_local(path)
    elapsed = time.perf_counter() - started
    table = p.tables[0]
    assert (table.total_rows, table.more_rows, len(table.rows)) == (1_000_000, True, 100)
    assert table.rows[99] == [99, 1.0, 1.0]
    assert varget_calls == [("counts", 0, 99), ("flux", 0, 99)]
    assert elapsed < 2


def test_damaged_cdf_is_an_error_without_paths(tmp_path: Path) -> None:
    path = tmp_path / "secret-dir" / "bad.cdf"
    path.parent.mkdir()
    path.write_bytes(b"this is not a CDF file at all")
    p = preview_local(path)
    assert p.kind == "error"
    assert p.message
    assert "secret-dir" not in p.message
    assert str(tmp_path) not in p.message


# -- storage without local paths ------------------------------------------------


@pytest.fixture
def memory_cdf(sample: Path) -> Iterator[tuple[fsspec.AbstractFileSystem, str, int]]:
    fs = fsspec.filesystem("memory")
    path = f"/hp-preview-{uuid.uuid4().hex}/sample.cdf"
    data = sample.read_bytes()
    fs.pipe_file(path, data)
    yield fs, path, len(data)
    fs.rm(path)


def test_local_path_only_for_local_filesystems(tmp_path: Path) -> None:
    assert backends.local_path(fsspec.filesystem("file"), str(tmp_path / "a")) == str(
        tmp_path / "a"
    )
    assert backends.local_path(fsspec.filesystem("memory"), "/a/b.cdf") is None


def test_remote_cdf_over_the_limit_is_too_large_and_not_read(
    memory_cdf: tuple[fsspec.AbstractFileSystem, str, int],
) -> None:
    fs, path, size = memory_cdf
    assert backends.local_path(fs, path) is None
    opened: list[str] = []

    def opener() -> Any:
        opened.append(path)
        return fs.open(path, "rb")

    p = build_preview(file_id=1, name=path, opener=opener, size=size, max_bytes=size - 1)
    assert p.kind == "too_large"
    assert p.message and "CDF" in p.message
    assert opened == []


def test_remote_cdf_within_the_limit_is_copied_then_removed(
    memory_cdf: tuple[fsspec.AbstractFileSystem, str, int],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fs, path, size = memory_cdf
    temp_dir = tmp_path / "temp"
    temp_dir.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_dir))
    p = build_preview(
        file_id=1, name=path, opener=lambda: fs.open(path, "rb"), size=size, max_bytes=size
    )
    assert p.kind == "table"
    assert [t.name for t in p.tables] == ["5 records", "3 records", "non-record-varying"]
    assert list(temp_dir.iterdir()) == []


def test_remote_cdf_longer_than_reported_stops_at_the_limit(
    memory_cdf: tuple[fsspec.AbstractFileSystem, str, int],
) -> None:
    fs, path, _ = memory_cdf
    p = build_preview(
        file_id=1, name=path, opener=lambda: fs.open(path, "rb"), size=100, max_bytes=1000
    )
    assert p.kind == "too_large"


def test_remote_damaged_cdf_hides_the_temporary_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs = fsspec.filesystem("memory")
    path = f"/hp-preview-{uuid.uuid4().hex}/bad.cdf"
    fs.pipe_file(path, b"not a CDF")
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    try:
        p = build_preview(
            file_id=1, name=path, opener=lambda: fs.open(path, "rb"), size=9, max_bytes=GB
        )
    finally:
        fs.rm(path)
    assert p.kind == "error"
    assert p.message
    assert str(tmp_path) not in p.message
    assert "hp-preview-" not in p.message


def test_unreadable_variables_are_reported_and_the_rest_still_shown(tmp_path: Path) -> None:
    path = tmp_path / "partial.cdf"
    writer = CDFWriter(str(path), cdf_spec={"Compressed": 0})
    writer.write_var(_var("good", CDF_DOUBLE), {}, np.array([1.0, 2.0]))
    writer.write_var(_var("empty", CDF_DOUBLE), {})  # zero records: cdflib cannot read it
    writer.close()
    p = preview_local(path)
    assert p.kind == "table"
    assert [c.name for c in p.tables[0].columns] == ["good"]
    assert p.tables[0].rows == [[1.0], [2.0]]
    assert p.message == "1 variable could not be read and is not shown: empty"


def test_cdf_is_read_through_crump_with_bounded_records(
    sample: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import crump

    from heliopuddle.preview import cdf as cdf_preview

    seen: list[Any] = []
    original = crump.read_cdf_variables

    def spy(file_path: Any, max_records: Any = None, skipped: Any = None) -> Any:
        seen.append(max_records)
        return original(file_path, max_records=max_records, skipped=skipped)

    monkeypatch.setattr(cdf_preview, "read_cdf_variables", spy)
    preview_local(sample)
    assert seen == [100]

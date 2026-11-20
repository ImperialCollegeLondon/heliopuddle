"""CDF previews: global attributes and one table per group of variables with one record count.

The reading is crump's (:func:`crump.read_cdf_variables` with ``max_records``,
so at most :data:`MAX_ROWS` records of any variable are loaded; epochs become
datetimes; unreadable variables are reported, not fatal), as are the global
attributes, the grouping of record-varying variables into tables by record
count (:func:`crump.group_variables_by_record_count`, largest first) and the
column names of 2-D variables (:meth:`crump.CDFVariable.get_column_names`).
Non-record-varying variables form their own one-row table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cdflib
import numpy as np
from crump import (
    CDFVariable,
    group_variables_by_record_count,
    read_cdf_global_attributes,
    read_cdf_variables,
)

from heliopuddle.preview.cells import MAX_ROWS, Content, attributes_text, make_table
from heliopuddle.server.schemas.preview import PreviewColumn, PreviewTable, PreviewVariable

NON_RECORD_VARYING = "non-record-varying"
"""Name of the one-row table of variables that do not vary by record."""


class _FirstRecordOnly:
    """A view of a CDF whose ``varget`` reads at most one record (used for label variables)."""

    def __init__(self, cdf: cdflib.CDF) -> None:
        self._cdf = cdf

    def varget(self, variable: str) -> Any:
        inq = self._cdf.varinq(variable)
        if inq.Rec_Vary and inq.Last_Rec >= 0:
            return self._cdf.varget(variable, startrec=0, endrec=0)
        return self._cdf.varget(variable)


@dataclass
class _Group:
    records: int
    columns: list[PreviewColumn] = field(default_factory=list)
    values: list[list[Any]] = field(default_factory=list)
    variables: list[PreviewVariable] = field(default_factory=list)


def _columns(
    cdf: cdflib.CDF, variable: CDFVariable, array: np.ndarray
) -> tuple[list[str], list[list[Any]]]:
    """Column names and per-column values (one list per column) of one variable.

    ``array`` holds the records shown, one per row: 1-D is one column, 2-D one
    column per element (named by crump from the label attributes), anything
    deeper one column with each record's value.
    """
    name = variable.name
    if array.ndim == 2:
        width = array.shape[1]
        named = CDFVariable(
            name=name,
            data=array,
            num_records=variable.num_records,
            shape=array.shape,
            dtype=str(array.dtype),
            attributes=variable.attributes,
        )
        try:
            names = named.get_column_names(_FirstRecordOnly(cdf))
        except Exception:
            names = []
        if len(names) != width:
            names = [f"{name}_{i}" for i in range(width)]
        return names, [list(array[:, i]) for i in range(width)]
    return [name], [list(array)]


def _records(variable: CDFVariable, rec_vary: bool) -> np.ndarray:
    """The variable's data as an array with one row per record shown."""
    array = np.asarray(variable.data)
    if rec_vary:
        return array if array.ndim else array.reshape(1)
    # A non-record-varying variable is one row: a scalar, or one array value.
    return array.reshape((1, -1)) if array.ndim else array.reshape(1)


def preview_cdf(path: str) -> Content:
    """Global attributes and the first 100 records of each group of variables.

    Args:
        path: a local path of the CDF file.

    Raises:
        Exception: cdflib's error when the file is not a readable CDF.
    """
    file = Path(path)
    skipped: list[str] = []
    variables = read_cdf_variables(file, max_records=MAX_ROWS, skipped=skipped)
    global_attributes = attributes_text(read_cdf_global_attributes(file))
    tables: list[PreviewTable] = []
    with cdflib.CDF(path) as cdf:
        inquiries = {v.name: cdf.varinq(v.name) for v in variables}
        varying = [v for v in variables if inquiries[v.name].Rec_Vary]
        fixed = [v for v in variables if not inquiries[v.name].Rec_Vary]
        for records, members in group_variables_by_record_count(varying).items():
            group = _group(cdf, records, members, inquiries, rec_vary=True)
            tables.append(_table(f"{records} records", group))
        if fixed:
            tables.append(
                _table(NON_RECORD_VARYING, _group(cdf, 1, fixed, inquiries, rec_vary=False))
            )
    message = None
    if skipped:
        count = len(skipped)
        message = (
            f"{count} variable{'s' if count != 1 else ''} could not be read and "
            f"{'are' if count != 1 else 'is'} not shown: {', '.join(skipped)}"
        )
    return Content(kind="table", tables=tables, attributes=global_attributes, message=message)


def _group(
    cdf: cdflib.CDF,
    records: int,
    members: list[CDFVariable],
    inquiries: dict[str, Any],
    *,
    rec_vary: bool,
) -> _Group:
    group = _Group(records)
    for variable in members:
        inq = inquiries[variable.name]
        kind = str(inq.Data_Type_Description)
        group.variables.append(
            PreviewVariable(
                name=variable.name,
                type=kind,
                shape=[int(d) for d in inq.Dim_Sizes],
                records=variable.num_records if rec_vary else 1,
                attributes=attributes_text(variable.attributes or {}),
            )
        )
        names, values = _columns(cdf, variable, _records(variable, rec_vary))
        group.columns.extend(PreviewColumn(name=n, type=kind) for n in names)
        group.values.extend(values)
    return group


def _table(name: str, group: _Group) -> PreviewTable:
    shown = min(group.records, MAX_ROWS)
    rows = [[col[i] if i < len(col) else None for col in group.values] for i in range(shown)]
    return make_table(
        group.columns,
        rows,
        more_rows=group.records > shown,
        name=name,
        total_rows=group.records,
        variables=group.variables,
    )

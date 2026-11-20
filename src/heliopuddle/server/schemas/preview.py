"""Schemas for file content previews (API-18)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

PreviewKind = Literal["table", "json", "text", "markdown", "image", "none", "too_large", "error"]
"""What the preview holds.

``table``: one or more tables (CSV, TSV, JSON arrays, JSON lines, Parquet, CDF).
``json``: pretty-printed JSON in ``text``. ``text`` / ``markdown``: the start of
the file in ``text``. ``image``: no content here; fetch the bytes. ``none``: no
preview for this format. ``too_large``: the format needs more of the file than
the preview limit allows. ``error``: the file could not be read as its format.
"""

Cell = str | int | float | bool | None
"""One table cell. Non-finite floats, dates, bytes and arrays arrive as strings."""


class PreviewColumn(BaseModel):
    """One column of a preview table."""

    name: str
    type: str | None = Field(default=None, description="The source type, when the format has one")


class PreviewVariable(BaseModel):
    """One variable of a CDF file, with its attributes."""

    name: str
    type: str
    shape: list[int] = Field(description="Shape of one record's value; empty for scalars")
    records: int
    attributes: dict[str, str] = Field(default_factory=dict)


class PreviewTable(BaseModel):
    """The first rows of one table."""

    name: str | None = None
    columns: list[PreviewColumn]
    rows: list[list[Cell]]
    total_rows: int | None = Field(
        default=None, description="All rows in the table, when known without reading it all"
    )
    more_rows: bool = Field(description="The table has rows after those shown")
    hidden_columns: int = Field(default=0, description="Columns left out beyond the limit")
    variables: list[PreviewVariable] = Field(
        default_factory=list, description="CDF: the variables this table's columns come from"
    )


class FilePreview(BaseModel):
    """A preview of a file's content (API-18)."""

    file_id: int
    kind: PreviewKind
    format: str | None = Field(default=None, description="e.g. csv, json, parquet, cdf, png")
    media_type: str | None = None
    storage_location: str | None = Field(
        default=None, description="The storage location whose copy was read"
    )
    max_rows: int = 100
    tables: list[PreviewTable] = Field(default_factory=list)
    text: str | None = None
    text_truncated: bool = False
    attributes: dict[str, str] = Field(
        default_factory=dict,
        description="File-level attributes: CDF global attributes, Parquet key-value metadata",
    )
    message: str | None = Field(default=None, description="Why there is no preview, or the error")

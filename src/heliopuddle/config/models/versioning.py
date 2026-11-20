"""Version rules: how a path yields a series and a version."""

from __future__ import annotations

import re
import string
from functools import cached_property
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
)

VersionType = Literal["number", "text"]

_RESERVED_DATE_GROUP = "date"


def template_fields(template: str) -> list[str]:
    """Return the field names of a ``version_text`` format template, in order.

    Raises:
        ValueError: if the template does not parse, or a field is positional
            (``{}``, ``{0}``) or uses attribute or index access.
    """
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as e:
        raise ValueError(f"invalid format template: {e}") from e
    fields: list[str] = []
    for _, field, spec, _ in parsed:
        if field is None:
            continue
        if not field.isidentifier():
            raise ValueError(f"template field {field!r} must be the name of a group")
        if spec and "{" in spec:
            raise ValueError(f"template field {field!r} must not nest fields in its format spec")
        fields.append(field)
    return fields


def _pattern_groups(info: ValidationInfo) -> set[str] | None:
    """Return the named groups of the already-validated ``extraction_pattern``."""
    pattern = info.data.get("extraction_pattern")
    return None if pattern is None else set(re.compile(pattern).groupindex)


class VersionRule(BaseModel):
    """A version rule: for paths matching ``paths``, search ``extraction_pattern``
    in the file name and take a number or text version from the match.

    See docs/design/versioning.md.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    paths: list[str] = Field(min_length=1, description="Globs on the relative path")
    version_type: VersionType = Field(
        default="number", description="number (an integer) or text (a string)"
    )
    extraction_pattern: str = Field(min_length=1, description="Regex searched in the file name")
    version_text: str | None = Field(
        default=None,
        min_length=1,
        description="text rules only: Python format string over the pattern's named groups",
    )
    ignore_in_series: list[str] = Field(
        default_factory=list, description="Named groups that do not split a series, e.g. ext"
    )

    @field_validator("paths")
    @classmethod
    def _non_empty_globs(cls, v: list[str]) -> list[str]:
        if any(not p.strip() for p in v):
            raise ValueError("globs must not be empty")
        return v

    @field_validator("extraction_pattern")
    @classmethod
    def _valid_pattern(cls, v: str) -> str:
        try:
            re.compile(v)
        except re.error as e:
            raise ValueError(f"invalid regex: {e}") from e
        return v

    # The group checks are field validators (not one model validator) so an
    # error's location names the field: versioning.<index>.<field> (DEP-06).
    # Fields validate in order, so ``info.data`` holds the earlier valid ones.

    @field_validator("version_text")
    @classmethod
    def _valid_version_text(cls, v: str | None, info: ValidationInfo) -> str | None:
        if v is None:
            return v
        if info.data.get("version_type", "text") != "text":
            raise ValueError("version_text is only allowed with version_type: text")
        fields = template_fields(v)
        groups = _pattern_groups(info)
        if groups is None:  # extraction_pattern is itself invalid
            return v
        for field in fields:
            if field == _RESERVED_DATE_GROUP:
                raise ValueError("version_text must not use the 'date' group")
            if field not in groups:
                raise ValueError(
                    f"version_text uses {{{field}}}, which is not a named group of "
                    "extraction_pattern"
                )
        try:
            v.format(**{g: "0" for g in groups})
        except (ValueError, TypeError, KeyError, IndexError) as e:
            raise ValueError(f"version_text does not format: {e}") from e
        return v

    @field_validator("ignore_in_series")
    @classmethod
    def _valid_ignore_in_series(cls, v: list[str], info: ValidationInfo) -> list[str]:
        groups = _pattern_groups(info)
        for group in v:
            if group == _RESERVED_DATE_GROUP:
                raise ValueError("ignore_in_series must not list the 'date' group")
            if groups is not None and group not in groups:
                raise ValueError(
                    f"ignore_in_series lists {group!r}, which is not a named group of "
                    "extraction_pattern"
                )
        return v

    @cached_property
    def compiled(self) -> re.Pattern[str]:
        """The compiled extraction pattern."""
        return re.compile(self.extraction_pattern)

    @cached_property
    def version_groups(self) -> tuple[str, ...]:
        """Named groups whose text is the version; empty when it is the whole match."""
        if self.version_text is not None:
            return tuple(dict.fromkeys(template_fields(self.version_text)))
        if "version" in self.compiled.groupindex:
            return ("version",)
        return ()


DEFAULT_VERSION_RULES: tuple[VersionRule, ...] = (
    VersionRule(paths=["**"], extraction_pattern=r"_[vV](?P<version>\d{1,5})\.[^./]+$"),
)
"""The rules of a project that does not set ``versioning``."""

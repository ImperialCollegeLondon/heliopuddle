"""Derive a file's series and version from its path. See docs/design/versioning.md."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from heliopuddle.config.models import VersionRule, VersionType
from heliopuddle.globs import glob_match

GENERATED_PREFIX = "v:"
"""Series key prefix of keys generated from a version rule (API-13)."""
LITERAL_PREFIX = "p:"
"""Series key prefix of an unversioned (literal) path (API-13)."""

MAX_VERSION_NUMBER = 2**63 - 1
"""The largest ``version_number`` (a signed 64-bit integer)."""
MAX_VERSION_TEXT = 255
"""The longest ``version_text``."""

_WHOLE_MATCH = "{v}"


class InvalidVersionError(ValueError):
    """A version rule extracted a value that is not a valid version (DEP-12).

    Attributes:
        rule_index: index of the rule in the project's ``versioning`` list.
        group: the named group that held the value, or ``None`` for the whole
            match or a ``version_text`` template.
        value: the extracted text.
    """

    def __init__(self, rule_index: int, group: str | None, value: str, reason: str) -> None:
        where = f"group {group!r}" if group else "the version"
        super().__init__(f"version rule {rule_index}: {where} gave {value!r}, which {reason}")
        self.rule_index = rule_index
        self.group = group
        self.value = value


def display_series_key(series_key: str) -> str:
    """Return a stored series key without its ``v:``/``p:`` namespace prefix."""
    if series_key.startswith((GENERATED_PREFIX, LITERAL_PREFIX)):
        return series_key[len(GENERATED_PREFIX) :]
    return series_key


@dataclass(frozen=True)
class VersionInfo:
    """The series and version of one path.

    Attributes:
        series_key: the stored series key: ``v:`` plus the path with its
            version parts replaced by placeholders, or ``p:`` plus the path
            when no rule matched (API-13).
        version_number: the number version, or None.
        version_text: the text version, or None.
        content_date: from a ``date`` group, or None.
    """

    series_key: str
    version_number: int | None = None
    version_text: str | None = None
    content_date: datetime | None = None

    @property
    def version_type(self) -> VersionType | None:
        """``number``, ``text``, or None for an unversioned path."""
        if self.version_number is not None:
            return "number"
        if self.version_text is not None:
            return "text"
        return None

    @property
    def is_versioned(self) -> bool:
        """True when the path has a number or text version."""
        return self.version_type is not None


def literal(path: str) -> VersionInfo:
    """Return the unversioned info of ``path``: its literal series and no version."""
    return VersionInfo(series_key=LITERAL_PREFIX + path)


def derive(path: str, rules: Sequence[VersionRule], *, version: bool = True) -> VersionInfo:
    """Return the series and version of ``path`` (first matching rule wins).

    Args:
        path: the relative path.
        rules: the project's version rules (``ProjectConfig.versioning``).
        version: extract and validate the version. False returns only the
            series key and content date, for a path whose version is not taken
            from the rules (an existing file, or a client override).

    Raises:
        InvalidVersionError: ``version`` is true and the matching rule
            extracted a value that is not a valid version of its type.
    """
    name = path.rsplit("/", 1)[-1]
    dir_len = len(path) - len(name)
    for index, rule in enumerate(rules):
        if not any(glob_match(path, p) for p in rule.paths):
            continue
        m = rule.compiled.search(name)
        if m is None:
            continue
        number, text = _extract(index, rule, m) if version else (None, None)
        key = _series_name(name, rule, m)
        return VersionInfo(
            series_key=GENERATED_PREFIX + path[:dir_len] + key,
            version_number=number,
            version_text=text,
            content_date=_parse_date(m.groupdict().get("date")),
        )
    return literal(path)


def apply_override(
    info: VersionInfo, version_number: int | None, version_text: str | None
) -> VersionInfo:
    """Return ``info`` with the client's version override, if any (API-20).

    The series key is kept: it always comes from the path.

    Raises:
        ValueError: if both overrides are given.
    """
    if version_number is not None and version_text is not None:
        raise ValueError("give version_number or version_text, not both")
    if version_number is None and version_text is None:
        return info
    return replace(info, version_number=version_number, version_text=version_text)


def _extract(index: int, rule: VersionRule, m: re.Match[str]) -> tuple[int | None, str | None]:
    groups = m.groupdict()
    if rule.version_text is not None:
        value = rule.version_text.format(**{k: (v or "") for k, v in groups.items()})
        group = None
    elif "version" in rule.compiled.groupindex:
        value, group = groups["version"] or "", "version"
    else:
        value, group = m.group(0), None
    if rule.version_type == "number":
        if not (value.isascii() and value.isdigit()):
            raise InvalidVersionError(index, group, value, "is not an integer")
        number = int(value)
        if number > MAX_VERSION_NUMBER:
            raise InvalidVersionError(index, group, value, "is too large")
        return number, None
    if not value:
        raise InvalidVersionError(index, group, value, "is empty")
    if len(value) > MAX_VERSION_TEXT:
        raise InvalidVersionError(
            index, group, value, f"is longer than {MAX_VERSION_TEXT} characters"
        )
    return None, value


def _series_name(name: str, rule: VersionRule, m: re.Match[str]) -> str:
    """Return ``name`` with the rule's version parts replaced by placeholders."""
    parts = rule.version_groups
    if not parts:
        spans = [(*m.span(0), _WHOLE_MATCH)]
    else:
        spans = [(*m.span(g), _placeholder(g)) for g in parts]
    spans += [(*m.span(g), _placeholder(g)) for g in rule.ignore_in_series]
    spans = [s for s in spans if s[0] >= 0]  # groups that did not take part
    if "date" in rule.compiled.groupindex:
        spans = _outside(spans, m.span("date"))
    # Drop any span inside an earlier one.
    kept: list[tuple[int, int, str]] = []
    for start, end, text in sorted(spans, key=lambda s: (s[0], -s[1])):
        if kept and start < kept[-1][1]:
            continue
        kept.append((start, end, text))
    key = name
    for start, end, text in reversed(kept):
        key = key[:start] + text + key[end:]
    return key


def _outside(
    spans: list[tuple[int, int, str]], date: tuple[int, int]
) -> list[tuple[int, int, str]]:
    """Cut the ``date`` group's span out of every span: a date is never a version part.

    Without this, a version span holding the date (e.g. a whole-match version
    such as ``20250101_v1``) would put files of different dates in one series.
    """
    lo, hi = date
    if lo < 0 or lo == hi:
        return spans
    cut: list[tuple[int, int, str]] = []
    for start, end, text in spans:
        if end <= lo or start >= hi:
            cut.append((start, end, text))
            continue
        if start < lo:
            cut.append((start, lo, text))
        if end > hi:
            cut.append((hi, end, text))
    return cut


def _placeholder(group: str) -> str:
    return _WHOLE_MATCH if group == "version" else "{" + group + "}"


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


__all__ = [
    "GENERATED_PREFIX",
    "LITERAL_PREFIX",
    "MAX_VERSION_NUMBER",
    "MAX_VERSION_TEXT",
    "InvalidVersionError",
    "VersionInfo",
    "apply_override",
    "derive",
    "display_series_key",
    "literal",
]

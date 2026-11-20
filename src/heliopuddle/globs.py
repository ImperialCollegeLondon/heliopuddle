"""Path globs: ``*`` (within one path segment), ``**`` (any depth) and ``?``.

Used by version rules (``paths``) and by the file list ``path`` filter.
"""

from __future__ import annotations

import re
from functools import lru_cache


@lru_cache(maxsize=1024)
def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Compile a glob matched against a whole relative path.

    ``**/`` matches zero or more directories, a trailing or standalone ``**``
    matches anything, ``*`` matches within one segment, ``?`` one character.
    """
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def glob_match(path: str, pattern: str) -> bool:
    """Return True if the whole ``path`` matches the glob ``pattern``."""
    return glob_to_regex(pattern).match(path) is not None


def literal_prefix(pattern: str) -> str:
    """Return the part of ``pattern`` before its first wildcard (for SQL ``LIKE``)."""
    m = re.search(r"[*?]", pattern)
    return pattern if m is None else pattern[: m.start()]

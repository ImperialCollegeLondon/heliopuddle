"""Building page URLs with query parameters."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from urllib.parse import urlencode


def files_url(base: str, params: Iterable[tuple[str, Any]]) -> str:
    """``base`` with the given query parameters (None values left out)."""
    pairs = [(k, str(v)) for k, v in params if v is not None and v != ""]
    return f"{base}?{urlencode(pairs)}" if pairs else base

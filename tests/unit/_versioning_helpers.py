"""Shared helpers and fixtures for the split versioning tests."""

from __future__ import annotations

from typing import Any

from heliopuddle.config.models import VersionRule


def _rule(pattern: str, **kwargs: Any) -> VersionRule:
    return VersionRule(paths=kwargs.pop("paths", ["**"]), extraction_pattern=pattern, **kwargs)

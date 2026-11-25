"""``{{ prefect.blocks.secret.<name> }}`` placeholders in storage location config (STO-18).

The syntax is Prefect's own (whitespace inside the braces is optional). Block
names follow Prefect's block document name rules: lowercase letters, digits
and dashes. The config loader only checks the syntax here; the values are
resolved lazily by :mod:`heliopuddle.storage.credentials`.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

__all__ = [
    "BLOCK_NAME_PATTERN",
    "PLACEHOLDER",
    "check_placeholders",
    "has_placeholders",
    "is_whole_placeholder",
    "walk_strings",
]

#: A Prefect block document name.
BLOCK_NAME_PATTERN = r"^[a-z0-9-]+$"

#: A Secret block placeholder; group 1 is the block name.
PLACEHOLDER = re.compile(r"\{\{\s*prefect\.blocks\.secret\.([a-z0-9-]+)\s*\}\}")


def is_whole_placeholder(value: Any) -> bool:
    """Whether ``value`` is a string that is exactly one placeholder."""
    return isinstance(value, str) and PLACEHOLDER.fullmatch(value.strip()) is not None


def walk_strings(value: Any, field: str) -> Iterator[tuple[str, str]]:
    """Yield ``(field, string)`` for every string in a nested config value."""
    if isinstance(value, str):
        yield field, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from walk_strings(item, f"{field}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from walk_strings(item, f"{field}.{index}")


def check_placeholders(value: Any, field: str) -> None:
    """Check the syntax of every ``{{ ... }}`` in the strings of ``value``.

    Args:
        value: a string or a nested mapping/list of config values.
        field: the field name used in the message.

    Raises:
        ValueError: naming the field, for a ``{{`` that does not start a valid
            ``{{ prefect.blocks.secret.<name> }}`` placeholder.
    """
    for where, text in walk_strings(value, field):
        rest = PLACEHOLDER.sub("", text)
        if "{{" in rest or "}}" in rest:
            raise ValueError(
                f"{where}: invalid placeholder in {text!r}: expected "
                "{{ prefect.blocks.secret.<name> }}, where <name> has only lowercase "
                "letters, digits and dashes"
            )


def has_placeholders(*values: Any) -> bool:
    """Whether any string in ``values`` holds a placeholder."""
    return any(PLACEHOLDER.search(text) for value in values for _, text in walk_strings(value, ""))
